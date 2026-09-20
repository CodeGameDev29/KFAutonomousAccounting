import { useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { parseISO } from "date-fns";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCategoryBreakdown } from "@/hooks/use-category-breakdown";
import { useTransactionDateRange } from "@/hooks/use-transaction-date-range";
import { DateRangePicker } from "@/components/analytics/DateRangePicker";
import { AnalyticsSummary } from "@/components/analytics/AnalyticsSummary";
import { ReasonablenessCard } from "@/components/analytics/ReasonablenessCard";
import { MonthlyTrendChart } from "@/components/analytics/MonthlyTrendChart";
import { CategoryTable } from "@/components/analytics/CategoryTable";
import { PeriodSelector } from "@/components/dashboard/PeriodSelector";
import {
  comparisonLabel,
  isCalendarMonth,
  monthPeriod,
  nextMonthPeriod,
  periodLabel,
  previousPeriod,
  prevMonthPeriod,
} from "@/lib/period";

export function Analytics() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

  const start = searchParams.get("start");
  const end = searchParams.get("end");
  const hasRange = start !== null && end !== null;

  const { data, isLoading, isFetching, error, refetch } = useCategoryBreakdown(
    start,
    end,
  );
  const { data: dateRange } = useTransactionDateRange();

  const prev = hasRange ? previousPeriod(start, end) : null;
  const prevQuery = useCategoryBreakdown(
    prev?.start ?? null,
    prev?.end ?? null,
    { enabled: prev !== null },
  );
  // undefined = comparison window unknown (loading, failed, or stale
  // keepPreviousData placeholder) — components then omit deltas instead of
  // claiming "no activity" or comparing against the wrong month.
  const prevCategories =
    prevQuery.isSuccess && !prevQuery.isPlaceholderData
      ? prevQuery.data.categories
      : undefined;

  // Backfill the URL with the backend's default period (latest month with
  // transactions) so refresh/share keeps the same view.
  useEffect(() => {
    if (!data?.default_period || start || end) return;
    const { year, month } = data.default_period;
    const period = monthPeriod(new Date(year, month - 1, 1));
    const next = new URLSearchParams(searchParams);
    next.set("start", period.start);
    next.set("end", period.end);
    setSearchParams(next, { replace: true });
  }, [data?.default_period, start, end, searchParams, setSearchParams]);

  const handleRangeChange = (next: { start: string; end: string }) => {
    const params = new URLSearchParams(searchParams);
    params.set("start", next.start);
    params.set("end", next.end);
    setSearchParams(params);
  };

  const handleCategoryClick = (category: string) => {
    if (!hasRange) return;
    navigate(
      `/transactions?category=${encodeURIComponent(category)}&start=${start}&end=${end}`,
    );
  };

  const isMonth = hasRange && isCalendarMonth(start, end);
  const rangeLabel = hasRange ? periodLabel(start, end) : "";
  const compareLabel = hasRange ? comparisonLabel(start, end) : "";
  const selectedMonthNum = isMonth ? parseISO(start).getMonth() + 1 : null;
  const trendYear = hasRange
    ? parseISO(end).getFullYear()
    : new Date().getFullYear();

  const now = new Date();
  const canGoNext =
    isMonth &&
    (parseISO(start).getFullYear() < now.getFullYear() ||
      (parseISO(start).getFullYear() === now.getFullYear() &&
        parseISO(start).getMonth() < now.getMonth()));

  const categories = data?.categories ?? [];
  // default_period only arrives on range-less requests, so also consult the
  // account-wide date range; while it loads, assume data exists to avoid
  // flashing the onboarding empty state at long-time users.
  const everHadTransactions =
    data?.default_period != null ||
    categories.length > 0 ||
    (dateRange ? dateRange.max_date != null : true);
  const showEmptyRange = !isLoading && hasRange && categories.length === 0;

  return (
    <div className="space-y-6 pt-4">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-foreground">
          Analytics
        </h2>
        <p className="text-muted-foreground mt-1">
          Where your money went{rangeLabel ? ` · ${rangeLabel}` : ""}
        </p>
      </div>

      {/* One filter row scoping everything below */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <DateRangePicker
          value={hasRange ? { start, end } : null}
          onChange={handleRangeChange}
          dataThrough={dateRange?.max_date}
        />
        {isMonth && (
          <PeriodSelector
            monthLabel={rangeLabel}
            onPrev={() => handleRangeChange(prevMonthPeriod(start))}
            onNext={() => handleRangeChange(nextMonthPeriod(start))}
            canGoNext={canGoNext}
          />
        )}
      </div>

      {error ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
          <p className="text-sm text-destructive font-medium">
            Failed to load analytics.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            className="mt-2"
          >
            Retry
          </Button>
        </div>
      ) : isLoading ? (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-[110px] rounded-xl" />
            ))}
          </div>
          <Skeleton className="h-[300px] rounded-xl" />
          <Skeleton className="h-[320px] rounded-xl" />
        </div>
      ) : !everHadTransactions ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-muted bg-muted/5 px-8 py-16 text-center">
          <p className="text-lg font-semibold text-foreground mb-2">
            No data yet
          </p>
          <p className="text-sm text-muted-foreground mb-6 max-w-md">
            Upload a bank statement or connect an account to see where your
            money goes.
          </p>
          <Button onClick={() => navigate("/onboarding")}>Get Started</Button>
        </div>
      ) : (
        <div
          className={
            isFetching ? "opacity-60 transition-opacity space-y-6" : "transition-opacity space-y-6"
          }
        >
          {showEmptyRange ? (
            <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-muted bg-muted/5 px-8 py-12 text-center">
              <p className="text-lg font-semibold text-foreground mb-2">
                No transactions in this range
              </p>
              <p className="text-sm text-muted-foreground mb-6 max-w-md">
                Pick a different range, or jump to a month with activity in the
                chart below.
              </p>
            </div>
          ) : (
            <AnalyticsSummary
              categories={categories}
              prevCategories={prevCategories}
              compareLabel={compareLabel}
              periodLabel={rangeLabel}
              isLoading={isLoading}
            />
          )}

          {!showEmptyRange && <ReasonablenessCard start={start} end={end} />}

          <MonthlyTrendChart
            year={trendYear}
            selectedMonth={selectedMonthNum}
            onSelectMonth={(year, month) =>
              handleRangeChange(monthPeriod(new Date(year, month - 1, 1)))
            }
          />

          {!showEmptyRange && (
            <CategoryTable
              categories={categories}
              prevCategories={prevCategories}
              compareLabel={compareLabel}
              onCategoryClick={handleCategoryClick}
            />
          )}
        </div>
      )}
    </div>
  );
}
