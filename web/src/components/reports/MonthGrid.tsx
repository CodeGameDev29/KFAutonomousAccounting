/** Month grid — 12-month overview for a fiscal year with completion status. */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Minus,
  FolderArchive,
  Loader2,
  Check,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import { MonthCardExport } from "@/components/reports/MonthCardExport";
import { TaxSummaryCard } from "@/components/reports/TaxSummaryCard";
import { DocumentCoverageBadge } from "@/components/reports/DocumentCoverageBadge";
import { useAuthStore } from "@/stores/auth-store";
import { downloadBinaryOrUrl } from "@/lib/downloadUtils";

interface MonthGridProps {
  year: number;
}

interface CurrencyMoney {
  currency: string;
  income: number;
  expenses: number;
  net: number;
}

interface MonthInfo {
  month: number;
  transaction_count: number;
  resolved_count: number;
  unresolved_count: number;
  reconcilable_count: number;
  income: number;
  expenses: number;
  by_currency?: CurrencyMoney[];
  status: string;
  closed: boolean;
}

interface MonthGridResponse {
  year: number;
  months: MonthInfo[];
}

const MONTH_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatCompact(amount: number, currency = "CAD"): string {
  const prefix =
    currency === "CAD" ? "C$" : currency === "USD" ? "US$" : `${currency} `;
  const absAmount = Math.abs(amount);
  if (absAmount >= 1000) {
    return `${prefix}${(absAmount / 1000).toFixed(1)}K`;
  }
  return `${prefix}${absAmount.toFixed(0)}`;
}

export function MonthGrid({ year }: MonthGridProps) {
  const navigate = useNavigate();
  const [fullYearState, setFullYearState] = useState<"idle" | "loading" | "done">("idle");

  const { data, isLoading, isError, error, refetch } = useQuery<MonthGridResponse>({
    queryKey: ["reports", year, "months"],
    queryFn: () => api.get(`/api/reports/${year}`),
  });

  return (
    <div className="space-y-6 pt-4">
      {/* Header — title left, year export right */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{year} Reports</h1>
          <p className="mt-1 text-muted-foreground">
            Monthly breakdown of transactions and reconciliation status.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="gap-1.5 border-primary/20 text-primary hover:bg-primary/5"
          disabled={fullYearState === "loading"}
          onClick={async () => {
              setFullYearState("loading");
              try {
                const token = await useAuthStore.getState().getAccessToken();
                await downloadBinaryOrUrl(
                  `/api/reports/${year}/binder-full-year`,
                  "GET",
                  token,
                  `TaxExport_FullYear_${year}.zip`
                );
                setFullYearState("done");
                setTimeout(() => setFullYearState("idle"), 2000);
              } catch (err) {
                console.error(err);
                toast.error("Full-year export failed — please try again.");
                setFullYearState("idle");
              }
            }}
          >
            {fullYearState === "loading" ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : fullYearState === "done" ? (
              <Check className="h-4 w-4" />
            ) : (
              <FolderArchive className="h-4 w-4" />
            )}
            {fullYearState === "done" ? "Exported" : "Export Full Year"}
          </Button>
      </div>

      {/* Month grid */}
      {isError ? (
        <ErrorDisplay error={error} context="monthly reports" onRetry={refetch} />
      ) : isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
          {Array.from({ length: 12 }).map((_, i) => (
            <Card key={i} className="shadow-warm rounded-xl">
              <CardContent className="p-5 space-y-3">
                <Skeleton className="h-5 w-20" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-24" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
          {Array.from({ length: 12 }, (_, i) => {
            const monthData = data?.months.find((m) => m.month === i + 1);
            const hasData = monthData && monthData.transaction_count > 0;
            const isClosed = monthData?.closed;
            const reconcilable = monthData?.reconcilable_count ?? monthData?.transaction_count ?? 0;
            const resolutionRate = monthData && reconcilable > 0
              ? Math.round((monthData.resolved_count / reconcilable) * 100)
              : 0;
            const unresolvedCount = monthData?.unresolved_count ?? 0;

            return (
              <Card
                key={i}
                className={cn(
                  "shadow-warm rounded-xl card-hover cursor-pointer transition-all",
                  hasData && isClosed && "border-l-3 accent-border-left",
                  hasData && !isClosed && "accent-border-amber",
                  !hasData && "opacity-60"
                )}
                onClick={() => navigate(`/reports/${year}/${i + 1}`)}
              >
                <CardContent className="p-5">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-lg font-bold tracking-tight">
                      {MONTH_SHORT[i]}
                    </h3>
                    {hasData && isClosed && (
                      <CheckCircle2 className="h-4.5 w-4.5 text-primary" />
                    )}
                    {hasData && !isClosed && unresolvedCount > 0 && (
                      <AlertCircle className="h-4.5 w-4.5 text-amber-500" />
                    )}
                    {hasData && !isClosed && unresolvedCount === 0 && (
                      <Clock className="h-4.5 w-4.5 text-muted-foreground" />
                    )}
                    {!hasData && (
                      <Minus className="h-4 w-4 text-muted-foreground/40" />
                    )}
                  </div>

                  {hasData ? (
                    <>
                      <p className="font-mono text-sm text-foreground">
                        {monthData.transaction_count} txns
                      </p>
                      <div className="mt-2 flex items-center gap-1.5">
                        {isClosed ? (
                          <>
                            <Badge className="bg-primary/10 text-primary border-primary/20 hover:bg-primary/10 text-xs">
                              Closed
                            </Badge>
                            <DocumentCoverageBadge matchRate={resolutionRate} />
                          </>
                        ) : resolutionRate === 100 ? (
                          <Badge className="bg-success/10 text-success border-success/20 hover:bg-success/10 text-xs">
                            {resolutionRate}% resolved
                          </Badge>
                        ) : (
                          <Badge className="bg-amber-50 text-amber-700 border-amber-200 hover:bg-amber-50 text-xs">
                            {resolutionRate}% resolved
                          </Badge>
                        )}
                      </div>
                      <div className="mt-3 h-1.5 w-full rounded-full bg-muted overflow-hidden">
                        <div
                          className={cn(
                            "h-full rounded-full transition-all duration-500",
                            isClosed ? "bg-primary" : resolutionRate === 100 ? "bg-success" : "bg-amber-500"
                          )}
                          style={{ width: `${resolutionRate}%` }}
                        />
                      </div>
                      {/* One line per currency — never sum mixed currencies */}
                      {(monthData.by_currency && monthData.by_currency.length > 0
                        ? monthData.by_currency
                        : [{ currency: "", income: monthData.income, expenses: monthData.expenses, net: 0 }]
                      ).map((c) => (
                        <p
                          key={c.currency || "legacy"}
                          className="mt-2 text-xs text-muted-foreground font-mono"
                        >
                          {c.currency
                            ? `${formatCompact(c.income, c.currency)} in / ${formatCompact(c.expenses, c.currency)} out`
                            : `$${Math.abs(c.income).toFixed(0)} in / $${Math.abs(c.expenses).toFixed(0)} out`}
                        </p>
                      ))}
                    </>
                  ) : (
                    <p className="text-xs text-muted-foreground mt-1">No data</p>
                  )}
                </CardContent>
                {hasData && (
                  <div
                    className="border-t px-5 py-2.5 flex justify-end bg-muted/10"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <MonthCardExport year={year} month={i + 1} />
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      )}

      {/* Sales-tax (GST/HST/PST) yearly summary from extracted receipts */}
      <TaxSummaryCard year={year} />
    </div>
  );
}
