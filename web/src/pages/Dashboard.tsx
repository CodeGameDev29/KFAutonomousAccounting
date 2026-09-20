import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { withParams } from "@/lib/url-state";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/auth-store";
import { Button } from "@/components/ui/button";
import { SummaryCards } from "@/components/dashboard/SummaryCards";
import { MonthlyStatus } from "@/components/dashboard/MonthlyStatus";
import { EmptyState } from "@/components/dashboard/EmptyState";
import { StatusBanner } from "@/components/dashboard/StatusBanner";
import { ActionQueue } from "@/components/dashboard/ActionQueue";
import { ConnectionHealthBanner } from "@/components/dashboard/ConnectionHealthBanner";
import { RecentTransactions } from "@/components/dashboard/RecentTransactions";
import { IncomeExpenseChart } from "@/components/dashboard/IncomeExpenseChart";
import { TopCategoriesWidget } from "@/components/dashboard/TopCategoriesWidget";
import { WidgetErrorBoundary } from "@/components/dashboard/WidgetErrorBoundary";
import { QuickExportCard } from "@/components/dashboard/QuickExportCard";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { ArrowRight, Sparkles, ChevronDown, ChevronUp, FileCheck, HelpCircle } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

interface OnboardingStatus {
  complete: boolean;
  has_profile: boolean;
}

interface DashboardSummary {
  total_transactions: number;
  resolved_count: number;
  unresolved_count: number;
  resolution_rate: number;
  income_this_month: number;
  expenses_this_month: number;
  net_this_month: number;
  month_label?: string;
}

interface ConnectionInfo {
  source: string;
  enabled: boolean;
  connected: boolean;
  status: string;
}

interface ConnectionsHealthResponse {
  connections: Record<string, ConnectionInfo>;
}

function getGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function getFirstName(user: { user_metadata?: Record<string, unknown> } | null): string | null {
  if (!user) return null;
  const fullName = user.user_metadata?.full_name as string | undefined;
  if (fullName) {
    return fullName.split(" ")[0];
  }
  return null;
}

export function Dashboard() {
  const user = useAuthStore((s) => s.user);
  const onboarded = useAuthStore((s) => s.onboarded);

  // Period + details toggle live in the URL so back/forward restores them.
  // Absent month/year = let SummaryCards fall back to the backend default.
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedMonth = (() => {
    const m = Number(searchParams.get("month"));
    return m >= 1 && m <= 12 ? m : null;
  })();
  const selectedYear = (() => {
    const y = Number(searchParams.get("year"));
    return y >= 2000 && y <= 2100 ? y : null;
  })();
  const showDetails = searchParams.get("details") !== "0";
  const setShowDetails = (v: boolean) =>
    setSearchParams((prev) => withParams(prev, { details: v ? null : "0" }), {
      replace: true,
    });
  const handlePeriodChange = (month: number, year: number) => {
    setSearchParams((prev) =>
      withParams(prev, { month: String(month), year: String(year) }),
    );
  };

  const { data: onboardingStatus, isLoading: onboardingLoading } =
    useQuery<OnboardingStatus>({
      queryKey: ["onboarding", "status"],
      queryFn: () => api.get("/api/onboarding/status"),
    });

  const { data: summary, isLoading: summaryLoading, isError: summaryError, error: summaryErrorObj, refetch: refetchSummary } =
    useQuery<DashboardSummary>({
      queryKey: ["dashboard", "summary"],
      queryFn: () => api.get("/api/dashboard/summary"),
    });

  const onboardingComplete =
    onboarded || (onboardingStatus?.complete === true);
  const showOnboardingBanner =
    !onboardingComplete &&
    !onboardingLoading &&
    onboardingStatus != null;

  const { data: actionItems } = useQuery<{
    items: Array<{
      type: string;
      count: number;
      message: string;
      action: string;
    }>;
  }>({
    queryKey: ["dashboard", "action-items"],
    queryFn: () => api.get("/api/dashboard/action-items"),
    enabled: onboardingComplete,
  });

  // Check connection health for graduated severity
  const { data: connectionsHealth } = useQuery<ConnectionsHealthResponse>({
    queryKey: ["connections", "health"],
    queryFn: () => api.get("/api/connections/health"),
    refetchInterval: 5 * 60 * 1000,
    enabled: onboardingComplete,
  });

  const hasConnectionIssues = connectionsHealth
    ? Object.values(connectionsHealth.connections).some(
        (c) =>
          c.enabled &&
          (c.status === "ERROR" || c.status === "DISCONNECTED")
      )
    : false;

  const hasData =
    !summaryLoading && summary && summary.total_transactions > 0;
  const isInitialLoad = summaryLoading;

  // Filter out onboarding from action queue (handled separately)
  const queueItems = actionItems?.items?.filter(
    (item) => item.type !== "onboarding"
  ) ?? [];

  // Personalized greeting
  const firstName = getFirstName(user);
  const greeting = getGreeting();
  const greetingText = firstName
    ? `${greeting}, ${firstName}`
    : greeting;

  const renderDashboardBody = () => {
    // (a) Onboarding incomplete
    if (showOnboardingBanner) {
      return (
        <div className="flex flex-col items-center justify-center rounded-xl border border-amber-600/20 bg-amber-600/5 px-8 py-12 shadow-warm-sm text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-600/10 mb-4">
            <Sparkles className="h-7 w-7 text-amber-600" />
          </div>
          <h3 className="text-lg font-semibold text-foreground mb-2">
            Complete your setup to get started
          </h3>
          <p className="text-sm text-muted-foreground mb-6 max-w-md">
            Upload your bank statements and first documents to start
            organizing your books.
          </p>
          <Button
            asChild
            className="gap-2 bg-amber-600 hover:bg-amber-700 text-white shadow-sm"
          >
            <Link to="/onboarding">
              Finish Setup
              <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        </div>
      );
    }

    // Error state
    if (summaryError) {
      return (
        <ErrorDisplay error={summaryErrorObj} context="dashboard data" onRetry={refetchSummary} />
      );
    }

    // Loading state
    if (isInitialLoad) {
      return (
        <div className="space-y-6">
          <Skeleton className="h-14 rounded-xl" />
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-[120px] rounded-xl" />
            ))}
          </div>
          <Skeleton className="h-[300px] rounded-xl" />
        </div>
      );
    }

    // (b) No data
    if (!hasData) {
      return <EmptyState />;
    }

    // (c) Has data: two-tier dashboard layout
    return (
      <>
        {/* === Always visible: the at-a-glance row === */}

        {/* Connection health banner (shown separately when issues exist) */}
        {hasConnectionIssues && (
          <WidgetErrorBoundary key="conn" widgetName="Connection Health">
            <ConnectionHealthBanner />
          </WidgetErrorBoundary>
        )}
        {/* One "what needs attention" surface: the action queue when items
            exist, the all-clear banner when nothing does. */}
        {queueItems.length > 0 ? (
          <WidgetErrorBoundary widgetName="Action Queue">
            <ActionQueue items={queueItems} />
          </WidgetErrorBoundary>
        ) : (
          !hasConnectionIssues && (
            <WidgetErrorBoundary key="status" widgetName="Status">
              <StatusBanner
                actionItems={queueItems}
                hasConnectionIssues={hasConnectionIssues}
                monthLabel={summary?.month_label}
              />
            </WidgetErrorBoundary>
          )
        )}

        {/* Summary cards */}
        <WidgetErrorBoundary widgetName="Summary">
          <SummaryCards
            selectedMonth={selectedMonth}
            selectedYear={selectedYear}
            onPeriodChange={handlePeriodChange}
          />
        </WidgetErrorBoundary>

        {/* Document coverage. The number is one measured thing — the share of
            transactions that have a matched document — and the copy says so
            rather than reading it as a compliance verdict. */}
        {summary && summary.total_transactions > 0 && (
          <WidgetErrorBoundary widgetName="Document coverage">
            <Card className="shadow-warm rounded-xl border-l-3 border-l-primary">
              <CardContent className="flex items-center gap-4 p-4">
                <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-primary/10">
                  <FileCheck className="h-5 w-5 text-primary" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <p className="text-sm font-semibold text-foreground">
                      Documented: {Math.round(summary.resolution_rate)}%
                    </p>
                    <Popover>
                      <PopoverTrigger asChild>
                        <button type="button" className="text-muted-foreground hover:text-foreground transition-colors" aria-label="What does this percentage measure?">
                          <HelpCircle className="h-3.5 w-3.5" />
                        </button>
                      </PopoverTrigger>
                      <PopoverContent className="w-80 text-sm" side="top" align="start">
                        <div className="space-y-2">
                          <p className="font-semibold text-foreground">What does this measure?</p>
                          <p className="text-muted-foreground">
                            The share of transactions in this period that have a
                            matched source document. It is a coverage figure, not
                            a compliance status: nobody has assessed these records
                            against the CRA&apos;s electronic record-keeping
                            guidance. Check with your accountant.
                          </p>
                          <a href="https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/ic05-1/electronic-record-keeping.html"
                             target="_blank" rel="noopener noreferrer"
                             className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
                            Read the CRA guidance (IC05-1R1)
                            <ArrowRight className="h-3 w-3" />
                          </a>
                        </div>
                      </PopoverContent>
                    </Popover>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {summary.resolved_count} of {summary.total_transactions} transactions have linked receipts
                  </p>
                </div>
                <div className="w-24 flex-shrink-0">
                  <div className="h-2 bg-muted rounded-full overflow-hidden">
                    <div
                      className="h-full bg-primary rounded-full transition-all duration-500"
                      style={{ width: `${Math.round(summary.resolution_rate)}%` }}
                    />
                  </div>
                </div>
              </CardContent>
            </Card>
          </WidgetErrorBoundary>
        )}

        {/* Recent transactions (always visible) */}
        <WidgetErrorBoundary widgetName="Recent Transactions">
          <RecentTransactions />
        </WidgetErrorBoundary>

        {/* Monthly status + income/expense chart (always visible).
            Each cell is its own `.hscroll` box: at phone width either widget's
            min-content width can exceed the viewport, and as bare grid items
            (min-width: auto) they would stretch the whole document sideways
            instead of scrolling inside themselves. */}
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="hscroll">
            <WidgetErrorBoundary widgetName="Monthly Status">
              <MonthlyStatus />
            </WidgetErrorBoundary>
          </div>
          <div className="hscroll">
            <WidgetErrorBoundary widgetName="Revenue vs Expenses">
              <IncomeExpenseChart />
            </WidgetErrorBoundary>
          </div>
        </div>

        {/* Quick Export */}
        {summary && summary.total_transactions > 0 && (
          <WidgetErrorBoundary widgetName="Quick Export">
            <QuickExportCard resolutionRate={summary.resolution_rate} />
          </WidgetErrorBoundary>
        )}

        {/* === Behind the toggle: the detailed widgets === */}
        <div>
          <button
            onClick={() => setShowDetails(!showDetails)}
            className="flex items-center gap-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
          >
            {showDetails ? (
              <>
                <ChevronUp className="h-4 w-4" />
                Hide category breakdown
              </>
            ) : (
              <>
                <ChevronDown className="h-4 w-4" />
                Show category breakdown
              </>
            )}
          </button>
        </div>

        {showDetails && (
          <WidgetErrorBoundary widgetName="Top Categories">
            <TopCategoriesWidget />
          </WidgetErrorBoundary>
        )}
      </>
    );
  };

  return (
    <div className="space-y-6 pt-4">
      {/* Personalized greeting header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-foreground">
            {greetingText}
          </h2>
          <p className="text-muted-foreground mt-1">
            Here&apos;s what needs your attention.
          </p>
        </div>
      </div>

      {/* Dashboard content */}
      {renderDashboardBody()}
    </div>
  );
}
