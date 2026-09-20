import { useSearchParams } from "react-router-dom";
import { withParams } from "@/lib/url-state";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CheckCircle2,
  AlertCircle,
  Clock,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";

interface CurrencyMoney {
  currency: string;
  income: number;
  expenses: number;
  net: number;
}

interface MonthStatus {
  month: number;
  transaction_count: number;
  resolved_count: number;
  unresolved_count: number;
  matched_count: number;
  linked_count: number;
  ignored_count: number;
  reconcilable_count: number;
  income: number;
  expenses: number;
  by_currency?: CurrencyMoney[];
  status: string;
}

interface MonthlyStatusResponse {
  year: number;
  months: MonthStatus[];
}

const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatCompact(amount: number, currency = ""): string {
  const prefix =
    currency === "CAD" ? "C$"
    : currency === "USD" ? "US$"
    : currency ? `${currency} `
    : "$";
  const absAmount = Math.abs(amount);
  if (absAmount >= 1000) {
    return `${prefix}${(absAmount / 1000).toFixed(0)}K`;
  }
  return `${prefix}${absAmount.toFixed(0)}`;
}

/**
 * Compact "in / out" money label for a month row. Uses the month's primary
 * currency and appends "+" when other currencies also have activity —
 * mixed-currency amounts are never summed, and income+expenses volume is
 * never presented as a single dollar figure.
 */
function monthMoneyLabel(month: MonthStatus): string {
  const list = month.by_currency ?? [];
  if (list.length === 0) {
    return `${formatCompact(month.income)} in · ${formatCompact(month.expenses)} out`;
  }
  const p = list[0];
  const more = list.length > 1 ? " +" : "";
  return `${formatCompact(p.income, p.currency)} in · ${formatCompact(p.expenses, p.currency)} out${more}`;
}

function getStatusInfo(status: string, resolvedCount: number, reconcilableCount: number, unresolvedCount: number) {
  // Map raw DB statuses to human-readable labels
  const normalized = status.toLowerCase();

  // If all reconcilable txns are resolved = complete
  if (resolvedCount === reconcilableCount && reconcilableCount > 0 && unresolvedCount === 0) {
    return {
      icon: CheckCircle2,
      label: "Complete",
      variant: "secondary" as const,
      color: "text-success",
      badgeClass: "bg-success/10 text-success border-success/20",
    };
  }

  if (unresolvedCount > 0) {
    return {
      icon: AlertCircle,
      label: `${unresolvedCount} unresolved`,
      variant: "outline" as const,
      color: "text-amber-500",
      badgeClass: "bg-amber-50 text-amber-700 border-amber-200",
    };
  }

  // Map raw statuses
  if (normalized === "closed" || normalized === "complete") {
    return {
      icon: CheckCircle2,
      label: "Complete",
      variant: "secondary" as const,
      color: "text-success",
      badgeClass: "bg-success/10 text-success border-success/20",
    };
  }

  // Default: in progress
  return {
    icon: Clock,
    label: "In progress",
    variant: "outline" as const,
    color: "text-muted-foreground",
    badgeClass: "",
  };
}

export function MonthlyStatus() {
  const now = new Date();
  // URL-backed so back/forward restores the year being viewed.
  const [searchParams, setSearchParams] = useSearchParams();
  const displayYear = (() => {
    const y = Number(searchParams.get("msy"));
    return y >= 2000 && y <= 2100 ? y : null;
  })();
  const setDisplayYear = (y: number) =>
    setSearchParams((prev) => withParams(prev, { msy: String(y) }));

  const { data, isLoading } = useQuery<MonthlyStatusResponse>({
    queryKey: ["dashboard", "monthly-status", displayYear],
    queryFn: () => {
      const params = displayYear ? `?year=${displayYear}` : "";
      return api.get(`/api/dashboard/monthly-status${params}`);
    },
  });

  const year = displayYear ?? data?.year ?? now.getFullYear();
  const canGoNext = year < now.getFullYear();

  // Filter out months with 0 transactions
  const activeMonths = data?.months?.filter(
    (m) => m.transaction_count > 0
  ) ?? [];

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-lg font-bold">Monthly Status</CardTitle>
            <CardDescription>
              Transaction reconciliation by month
            </CardDescription>
          </div>
          {/* Year navigation */}
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              onClick={() => setDisplayYear(year - 1)}
              aria-label="Previous year"
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <span className="text-sm font-semibold text-foreground min-w-[50px] text-center">
              {year}
            </span>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              onClick={() => setDisplayYear(year + 1)}
              disabled={!canGoNext}
              aria-label="Next year"
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {/* Legend */}
        <div className="flex items-center gap-4 mb-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <div className="h-2.5 w-5 rounded-full bg-primary" />
            <span>Resolved</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="h-2.5 w-5 rounded-full bg-primary/15" />
            <span>Unresolved</span>
          </div>
        </div>
        {isLoading || !data ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4">
                <Skeleton className="h-4 w-8" />
                <Skeleton className="h-4 flex-1" />
                <Skeleton className="h-5 w-16" />
              </div>
            ))}
          </div>
        ) : activeMonths.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No transaction data for {year}. Upload bank statements to get started.
          </p>
        ) : (
          <div className="space-y-3">
            {activeMonths.map((month) => {
              const maxTransactions = Math.max(
                ...activeMonths.map((m) => m.transaction_count),
                1
              );
              const progressPercent =
                (month.transaction_count / maxTransactions) * 100;
              const reconcilable = month.reconcilable_count ?? month.transaction_count;
              const resolvedPercent =
                reconcilable > 0
                  ? (month.resolved_count / reconcilable) * 100
                  : 0;
              const unresolvedCount = month.unresolved_count ?? (reconcilable - month.resolved_count);
              const statusInfo = getStatusInfo(
                month.status,
                month.resolved_count,
                reconcilable,
                unresolvedCount
              );
              const StatusIcon = statusInfo.icon;

              return (
                <Link
                  key={month.month}
                  to={`/reports/${year}/${month.month}`}
                  className="block group"
                >
                  <div className="space-y-1.5 rounded-lg px-2 py-2 -mx-2 transition-colors group-hover:bg-muted/30">
                    <div className="flex items-center justify-between text-sm">
                      <div className="flex items-center gap-2">
                        <span className="w-8 font-medium text-foreground">
                          {MONTH_NAMES[month.month - 1]}
                        </span>
                        {/* Inline resolved count — direct labeling */}
                        <span className="text-muted-foreground font-mono text-xs">
                          {month.resolved_count}/{reconcilable} resolved
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span
                          className="text-xs text-muted-foreground font-mono whitespace-nowrap"
                          title="Money in · money out (internal transfers excluded)"
                        >
                          {monthMoneyLabel(month)}
                        </span>
                        <Badge
                          variant={statusInfo.variant}
                          className={`gap-1 text-xs ${statusInfo.badgeClass}`}
                        >
                          <StatusIcon
                            className={`h-3 w-3 ${statusInfo.color}`}
                          />
                          {statusInfo.label}
                        </Badge>
                      </div>
                    </div>
                    {/* Progress bar — taller for readability */}
                    <div className="relative h-3 w-full rounded-full bg-muted overflow-hidden">
                      {/* Resolved portion (solid primary) */}
                      {month.resolved_count > 0 && (
                        <div
                          className="absolute inset-y-0 left-0 rounded-full bg-primary transition-all z-[2]"
                          style={{ width: `${progressPercent * resolvedPercent / 100}%` }}
                        />
                      )}
                      {/* Unresolved portion (light primary) */}
                      {unresolvedCount > 0 && (
                        <div
                          className="absolute inset-y-0 left-0 rounded-full bg-primary/15 transition-all z-[1]"
                          style={{ width: `${progressPercent}%` }}
                        />
                      )}
                    </div>
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
