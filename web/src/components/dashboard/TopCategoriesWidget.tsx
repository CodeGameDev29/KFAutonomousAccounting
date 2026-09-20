import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { withParams } from "@/lib/url-state";
import { endOfMonth, startOfMonth } from "date-fns";
import { periodLabel, toIso } from "@/lib/period";
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ArrowRight } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { useCategoryBreakdown } from "@/hooks/use-category-breakdown";
import { useTransactionDateRange } from "@/hooks/use-transaction-date-range";
import { DateRangePicker } from "@/components/analytics/DateRangePicker";
import type { DateRangeValue } from "@/components/analytics/DateRangePicker";
import type { CategoryBreakdown } from "@/types/analytics";
import { CHART_GREEN, formatSignedMoney } from "@/lib/money";

const SAGE = CHART_GREEN;
const ROSE = "hsl(var(--destructive))";

function currencyPrefix(currency: string): string {
  if (currency === "CAD") return "C$";
  if (currency === "USD") return "US$";
  return `${currency} `;
}

function makeCompactAxisFormatter(currency: string) {
  const prefix = currencyPrefix(currency);
  return (value: number): string => {
    const abs = Math.abs(value);
    const sign = value < 0 ? "\u2212" : "";
    if (abs >= 1_000_000) return `${sign}${prefix}${(abs / 1_000_000).toFixed(1)}M`;
    if (abs >= 1_000) return `${sign}${prefix}${(abs / 1_000).toFixed(1)}K`;
    return `${sign}${prefix}${abs.toFixed(0)}`;
  };
}

const formatSignedCurrency = formatSignedMoney;

function formatRangeLabel(range: DateRangeValue | null): string {
  if (!range) return "";
  return periodLabel(range.start, range.end);
}

interface ChartDatum extends CategoryBreakdown {
  rank: number;
}

interface CustomTooltipProps {
  active?: boolean;
  payload?: Array<{ payload: ChartDatum }>;
}

function CustomTooltip({ active, payload }: CustomTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded-lg border bg-white px-3 py-2 shadow-warm-md text-xs">
      <p className="font-semibold text-foreground mb-1">{d.category}</p>
      <p className="text-foreground">
        Net:{" "}
        <span className="font-mono font-semibold">
          {formatSignedCurrency(d.net, d.currency)}
        </span>
      </p>
      <p className="text-muted-foreground">
        Income: {formatSignedCurrency(d.income, d.currency)}
      </p>
      <p className="text-muted-foreground">
        Expenses: {formatSignedCurrency(d.expenses, d.currency)}
      </p>
      <p className="text-muted-foreground mt-1">
        {d.transaction_count} transactions
      </p>
    </div>
  );
}

export function TopCategoriesWidget() {
  const navigate = useNavigate();
  // URL-backed so back/forward restores the widget's range.
  const [searchParams, setSearchParams] = useSearchParams();
  const tcStart = searchParams.get("tcstart");
  const tcEnd = searchParams.get("tcend");
  const range: DateRangeValue | null =
    tcStart && tcEnd ? { start: tcStart, end: tcEnd } : null;
  const setRange = useCallback(
    (next: DateRangeValue, opts?: { replace?: boolean }) =>
      setSearchParams(
        (prev) => withParams(prev, { tcstart: next.start, tcend: next.end }),
        opts,
      ),
    [setSearchParams],
  );

  const { data, isLoading } = useCategoryBreakdown(
    range?.start ?? null,
    range?.end ?? null,
  );
  const { data: dateRange } = useTransactionDateRange();

  // Backfill the range from the backend's default_period (latest month with
  // transactions) on first load so the widget never lands on an empty month.
  useEffect(() => {
    if (range || !data?.default_period) return;
    const { year, month } = data.default_period;
    const first = new Date(year, month - 1, 1);
    setRange(
      {
        start: toIso(startOfMonth(first)),
        end: toIso(endOfMonth(first)),
      },
      { replace: true },
    );
  }, [data?.default_period, range, setRange]);

  // One currency is charted at a time — CAD and USD rows on one numeric
  // axis would rank and size them at par (a US$950 bar under a C$980 bar).
  const currencies = useMemo(() => {
    const set = new Set((data?.categories ?? []).map((c) => c.currency));
    return [...set].sort((a, b) => {
      const rank = (c: string) => (c === "CAD" ? 0 : c === "USD" ? 1 : 2);
      return rank(a) - rank(b) || a.localeCompare(b);
    });
  }, [data]);
  const [selectedCurrency, setSelectedCurrency] = useState<string | null>(null);
  const chartCurrency =
    selectedCurrency && currencies.includes(selectedCurrency)
      ? selectedCurrency
      : currencies[0] ?? "CAD";

  const topFive = useMemo<ChartDatum[]>(() => {
    const all = (data?.categories ?? []).filter(
      (c) => c.currency === chartCurrency,
    );
    const sorted = [...all]
      .sort((a, b) => Math.abs(b.net) - Math.abs(a.net))
      .slice(0, 5);
    return sorted.map((c, i) => ({ ...c, rank: i + 1 }));
  }, [data, chartCurrency]);

  const rangeLabel = formatRangeLabel(range);

  const title = useMemo(() => {
    if (topFive.length === 0) return "Top Profit/Loss Categories";
    const allNegative = topFive.every((c) => c.net <= 0);
    const prefix = `Top ${topFive.length}`;
    const suffix = currencies.length > 1 ? ` (${chartCurrency})` : "";
    if (allNegative) return `${prefix} Expense Categories${suffix}`;
    return `${prefix} Profit/Loss Categories${suffix}`;
  }, [topFive, currencies.length, chartCurrency]);

  const handleBarClick = (datum: ChartDatum) => {
    if (!range) return;
    navigate(
      `/transactions?category=${encodeURIComponent(datum.category)}&start=${range.start}&end=${range.end}`,
    );
  };

  const handleViewAll = () => {
    if (!range) {
      navigate("/analytics");
      return;
    }
    navigate(`/analytics?start=${range.start}&end=${range.end}`);
  };

  const header = (
    <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between space-y-0">
      <div>
        <CardTitle className="text-lg font-bold">{title}</CardTitle>
        <CardDescription>{rangeLabel || "Loading…"}</CardDescription>
      </div>
      <div className="flex flex-col items-start gap-2 sm:items-end">
        <div className="flex items-center gap-2">
          {currencies.length > 1 && (
            <div
              className="flex rounded-lg border bg-muted/30 p-0.5"
              role="tablist"
              aria-label="Chart currency"
            >
              {currencies.map((c) => (
                <button
                  key={c}
                  role="tab"
                  aria-selected={c === chartCurrency}
                  onClick={() => setSelectedCurrency(c)}
                  className={
                    c === chartCurrency
                      ? "rounded-md bg-white px-2.5 py-1 text-xs font-medium text-foreground shadow-sm"
                      : "rounded-md px-2.5 py-1 text-xs font-medium text-muted-foreground hover:text-foreground"
                  }
                >
                  {c}
                </button>
              ))}
            </div>
          )}
          <DateRangePicker
            value={range}
            onChange={setRange}
            dataThrough={dateRange?.max_date}
          />
        </div>
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-primary"
          onClick={handleViewAll}
        >
          View all categories
          <ArrowRight className="ml-1 h-3.5 w-3.5" />
        </Button>
      </div>
    </CardHeader>
  );

  if (isLoading && topFive.length === 0) {
    return (
      <Card className="shadow-warm rounded-xl">
        {header}
        <CardContent>
          <Skeleton className="h-[280px] w-full" />
        </CardContent>
      </Card>
    );
  }

  if (topFive.length === 0) {
    return (
      <Card className="shadow-warm rounded-xl">
        {header}
        <CardContent>
          <div className="flex h-[200px] items-center justify-center rounded-lg border border-dashed border-muted text-sm text-muted-foreground">
            No transaction activity for {rangeLabel || "this range"}.
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="shadow-warm rounded-xl">
      {header}
      <CardContent>
        <div
          role="img"
          aria-label={`Top ${topFive.length} categories bar chart for ${rangeLabel}`}
        >
          <ResponsiveContainer width="100%" height={320}>
            <BarChart
              data={topFive}
              layout="vertical"
              margin={{ top: 8, right: 24, left: 8, bottom: 8 }}
            >
              <XAxis
                type="number"
                tickFormatter={makeCompactAxisFormatter(chartCurrency)}
                tick={{ fontSize: 11, fill: "hsl(30, 5%, 47%)" }}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                type="category"
                dataKey="category"
                width={150}
                tickFormatter={(v: string) => (v.length > 18 ? v.slice(0, 17) + "…" : v)}
                tick={{ fontSize: 12, fill: "hsl(30, 5%, 30%)" }}
                tickLine={false}
                axisLine={false}
              />
              <Tooltip
                content={<CustomTooltip />}
                cursor={{ fill: "rgba(55, 53, 47, 0.04)" }}
              />
              <Bar
                dataKey="net"
                radius={[0, 4, 4, 0]}
                onClick={(payload: unknown) => {
                  const datum = payload as ChartDatum | undefined;
                  if (datum) handleBarClick(datum);
                }}
                cursor="pointer"
              >
                {topFive.map((c, i) => (
                  <Cell key={i} fill={c.net >= 0 ? SAGE : ROSE} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
