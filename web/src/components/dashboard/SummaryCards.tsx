import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { motion, useReducedMotion } from "motion/react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { PeriodSelector } from "./PeriodSelector";
import { ResolutionRateDonut } from "./ResolutionRateDonut";
import {
  ArrowLeftRight,
  TrendingUp,
  TrendingDown,
  DollarSign,
  Wallet,
  Minus,
} from "lucide-react";

interface CurrencyFinancials {
  currency: string;
  income: number;
  expenses: number;
  net: number;
}

interface DashboardSummary {
  total_transactions: number;
  resolved_count: number;
  unresolved_count: number;
  matched_count?: number;
  linked_count?: number;
  reconcilable_count?: number;
  ignored_count?: number;
  resolution_rate: number;
  income_this_month: number;
  expenses_this_month: number;
  net_this_month: number;
  income_prev_month?: number;
  expenses_prev_month?: number;
  net_prev_month?: number;
  income_ytd?: number;
  expenses_ytd?: number;
  net_ytd?: number;
  financials_this_month_by_currency?: CurrencyFinancials[];
  financials_prev_month_by_currency?: CurrencyFinancials[];
  financials_ytd_by_currency?: CurrencyFinancials[];
  month_label?: string;
  month?: number;
  year?: number;
}

function currencyPrefix(currency: string): string {
  if (currency === "CAD") return "C$";
  if (currency === "USD") return "US$";
  return `${currency} `;
}

function formatCompact(amount: number, currency = "CAD"): string {
  const absAmount = Math.abs(amount);
  const sign = amount < 0 ? "−" : "";
  const prefix = currencyPrefix(currency);
  if (absAmount >= 1000) {
    return `${sign}${prefix}${(absAmount / 1000).toFixed(1)}K`;
  }
  return `${sign}${prefix}${absAmount.toLocaleString("en-CA", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}`;
}

function TrendArrow({
  current,
  previous,
  moreIsGood = true,
}: {
  current: number;
  previous?: number;
  /** Is an increase good news (income/net) or bad news (expenses)? */
  moreIsGood?: boolean;
}) {
  if (previous === undefined || previous === 0) return null;
  const delta = ((current - previous) / Math.abs(previous)) * 100;
  // A near-zero prior month produces meaningless five-digit percentages
  // (e.g. "+30407%"). Past 999% the number carries no information — show a
  // qualitative badge instead.
  if (Math.abs(delta) > 999) {
    const isUpBig = delta > 0;
    return (
      <span
        className={`inline-flex items-center gap-0.5 text-xs font-medium ${
          isUpBig === moreIsGood ? "text-success" : "text-accent"
        }`}
        title="Change vs last month is too large to express as a percentage"
      >
        {isUpBig ? (
          <TrendingUp className="h-3 w-3" />
        ) : (
          <TrendingDown className="h-3 w-3" />
        )}
        {">999%"}
      </span>
    );
  }
  if (Math.abs(delta) < 0.5) {
    return (
      <span className="inline-flex items-center gap-0.5 text-xs text-muted-foreground">
        <Minus className="h-3 w-3" />
        flat
      </span>
    );
  }
  const isUp = delta > 0;
  return (
    <span
      className={`inline-flex items-center gap-0.5 text-xs font-medium ${
        isUp === moreIsGood ? "text-success" : "text-accent"
      }`}
    >
      {isUp ? (
        <TrendingUp className="h-3 w-3" />
      ) : (
        <TrendingDown className="h-3 w-3" />
      )}
      {Math.abs(Math.round(delta))}%
    </span>
  );
}

interface StatCardProps {
  title: string;
  value: string | null;
  description: string | React.ReactNode;
  icon: React.ComponentType<{ className?: string }>;
  accentColor?: "indigo" | "amber" | "sage" | "default";
  onClick?: () => void;
  trend?: React.ReactNode;
  rightContent?: React.ReactNode;
}

function StatCard({ title, value, description, icon: Icon, accentColor = "default", onClick, trend, rightContent }: StatCardProps) {
  const borderClass =
    accentColor === "indigo"
      ? "border-l-[3px] border-l-primary"
      : accentColor === "amber"
        ? "border-l-[3px] border-l-accent"
        : accentColor === "sage"
          ? "border-l-[3px] border-l-success"
          : "";

  const iconBg =
    accentColor === "indigo"
      ? "bg-primary/10 text-primary"
      : accentColor === "amber"
        ? "bg-accent/10 text-accent"
        : accentColor === "sage"
          ? "bg-success/10 text-success"
          : "bg-muted text-muted-foreground";

  const clickableClass = onClick
    ? "cursor-pointer transition-all hover:shadow-warm-md hover:-translate-y-0.5"
    : "";

  return (
    <Card
      className={`shadow-warm-sm bg-white ${borderClass} ${clickableClass}`}
      onClick={onClick}
    >
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
        {rightContent ?? (
          <div className={`flex h-8 w-8 items-center justify-center rounded-lg ${iconBg}`}>
            <Icon className="h-4 w-4" />
          </div>
        )}
      </CardHeader>
      <CardContent>
        {value !== null ? (
          <div className="font-mono text-3xl font-bold text-foreground tracking-tight">{value}</div>
        ) : (
          <Skeleton className="h-9 w-24" />
        )}
        <div className="flex items-center gap-2 mt-1">
          <p className="text-xs text-muted-foreground">{description}</p>
          {trend}
        </div>
      </CardContent>
    </Card>
  );
}

function StaggeredGrid({ children }: { children: React.ReactNode }) {
  const prefersReducedMotion = useReducedMotion();
  const items = Array.isArray(children) ? children : [children];

  if (prefersReducedMotion) {
    return (
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
        {children}
      </div>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
      {items.map((child, i) => (
        <motion.div
          key={i}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{
            duration: 0.3,
            delay: i * 0.08,
            ease: "easeOut",
          }}
        >
          {child}
        </motion.div>
      ))}
    </div>
  );
}

function primaryOf(list?: CurrencyFinancials[]): CurrencyFinancials | undefined {
  return list && list.length > 0 ? list[0] : undefined;
}

interface MoneyCardProps {
  metric: "income" | "expenses" | "net";
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  accentColor: "indigo" | "amber" | "sage" | "default";
  moreIsGood: boolean;
  data: DashboardSummary;
  monthLabel: string;
  currentYear: number;
  onClick: () => void;
}

/**
 * Income / Expenses / Net card. Money is shown PER CURRENCY — the headline
 * value is the primary currency (CAD first, then most active) and any other
 * currencies stack underneath. Different currencies are never numerically
 * summed or labeled as CAD.
 */
function MoneyCard({
  metric,
  title,
  icon,
  accentColor,
  moreIsGood,
  data,
  monthLabel,
  currentYear,
  onClick,
}: MoneyCardProps) {
  const byCur = data.financials_this_month_by_currency;
  const prevByCur = data.financials_prev_month_by_currency;
  const ytdByCur = data.financials_ytd_by_currency;

  // Fallback for a stale backend without per-currency data: legacy scalars
  // with a bare $ (never claim a specific currency for a mixed sum).
  if (!byCur) {
    const legacy = {
      income: data.income_this_month,
      expenses: data.expenses_this_month,
      net: data.net_this_month,
    }[metric] ?? 0;
    return (
      <StatCard
        title={title}
        value={formatCompact(legacy).replace("C$", "$")}
        description={monthLabel}
        icon={icon}
        accentColor={accentColor}
        onClick={onClick}
      />
    );
  }

  const primary = primaryOf(byCur);
  const others = byCur.slice(1);
  const primaryCur = primary?.currency ?? "CAD";
  const prevPrimary = prevByCur?.find((c) => c.currency === primaryCur);
  const ytdPrimary = ytdByCur?.find((c) => c.currency === primaryCur);

  const ytdText = ytdPrimary
    ? ` · ${currentYear} YTD: ${formatCompact(ytdPrimary[metric], primaryCur)}`
    : "";

  return (
    <StatCard
      title={byCur.length > 1 ? title : `${title} (${primaryCur})`}
      value={formatCompact(primary?.[metric] ?? 0, primaryCur)}
      description={
        <span className="flex flex-col gap-0.5">
          {others.map((c) => (
            <span key={c.currency} className="font-mono font-semibold text-foreground">
              {formatCompact(c[metric], c.currency)}
            </span>
          ))}
          <span>
            {monthLabel}
            {ytdText}
          </span>
        </span>
      }
      icon={icon}
      accentColor={accentColor}
      onClick={onClick}
      trend={
        <TrendArrow
          current={primary?.[metric] ?? 0}
          previous={prevPrimary?.[metric]}
          moreIsGood={moreIsGood}
        />
      }
    />
  );
}

interface SummaryCardsProps {
  selectedMonth: number | null;
  selectedYear: number | null;
  onPeriodChange: (month: number, year: number) => void;
}

export function SummaryCards({
  selectedMonth,
  selectedYear,
  onPeriodChange,
}: SummaryCardsProps) {
  const navigate = useNavigate();
  const now = new Date();

  const queryParams =
    selectedMonth && selectedYear
      ? `?month=${selectedMonth}&year=${selectedYear}`
      : "";

  const { data, isLoading } = useQuery<DashboardSummary>({
    queryKey: ["dashboard", "summary", selectedMonth, selectedYear],
    queryFn: () => api.get(`/api/dashboard/summary${queryParams}`),
  });

  // Period navigation
  const currentMonth = data?.month ?? now.getMonth() + 1;
  const currentYear = data?.year ?? now.getFullYear();
  const monthLabel = data?.month_label ?? "Loading...";
  const canGoNext =
    currentYear < now.getFullYear() ||
    (currentYear === now.getFullYear() && currentMonth < now.getMonth() + 1);

  const goToPrev = () => {
    const m = selectedMonth ?? currentMonth;
    const y = selectedYear ?? currentYear;
    if (m === 1) onPeriodChange(12, y - 1);
    else onPeriodChange(m - 1, y);
  };

  const goToNext = () => {
    const m = selectedMonth ?? currentMonth;
    const y = selectedYear ?? currentYear;
    if (m === 12) onPeriodChange(1, y + 1);
    else onPeriodChange(m + 1, y);
  };

  if (isLoading || !data) {
    return (
      <div className="space-y-3">
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <StatCard
              key={i}
              title={
                ["Transactions", "Transactions Resolved", "Income", "Expenses", "Net Income"][i]
              }
              value={null}
              description=""
              icon={
                [ArrowLeftRight, TrendingUp, DollarSign, Wallet, TrendingUp][i]
              }
              accentColor={
                (["indigo", "indigo", "sage", "amber", "sage"] as const)[i]
              }
            />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3" aria-live="polite">
      {/* Period selector */}
      <div className="flex items-center justify-between">
        <PeriodSelector
          monthLabel={monthLabel}
          onPrev={goToPrev}
          onNext={goToNext}
          canGoNext={canGoNext}
        />
      </div>

      <StaggeredGrid>
        <StatCard
          title="Transactions"
          value={(data.total_transactions ?? 0).toLocaleString()}
          description={`${currentYear} total`}
          icon={ArrowLeftRight}
          accentColor="indigo"
          onClick={() => navigate("/transactions")}
        />
        <StatCard
          title="Transactions Resolved"
          value={`${Math.round(data.resolution_rate ?? 0)}%`}
          description={`${data.resolved_count ?? 0} of ${data.reconcilable_count ?? data.total_transactions ?? 0} resolved`}
          icon={TrendingUp}
          accentColor={(data.resolution_rate ?? 0) < 80 ? "amber" : "indigo"}
          onClick={() => navigate("/transactions?filter=unmatched")}
          rightContent={
            <ResolutionRateDonut
              resolved={data.resolved_count ?? 0}
              total={data.reconcilable_count ?? data.total_transactions ?? 0}
            />
          }
        />
        <MoneyCard
          metric="income"
          title="Income"
          icon={DollarSign}
          accentColor="sage"
          moreIsGood
          data={data}
          monthLabel={monthLabel}
          currentYear={currentYear}
          onClick={() => navigate(`/reports/${currentYear}/${currentMonth}`)}
        />
        <MoneyCard
          metric="expenses"
          title="Expenses"
          icon={Wallet}
          accentColor="amber"
          moreIsGood={false}
          data={data}
          monthLabel={monthLabel}
          currentYear={currentYear}
          onClick={() => navigate(`/reports/${currentYear}/${currentMonth}`)}
        />
        <MoneyCard
          metric="net"
          title="Net Income"
          icon={TrendingUp}
          accentColor={(primaryOf(data.financials_this_month_by_currency)?.net ?? data.net_this_month ?? 0) >= 0 ? "sage" : "amber"}
          moreIsGood
          data={data}
          monthLabel={monthLabel}
          currentYear={currentYear}
          onClick={() => navigate(`/reports/${currentYear}/${currentMonth}`)}
        />
      </StaggeredGrid>
    </div>
  );
}
