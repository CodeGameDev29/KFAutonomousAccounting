import { useState, useCallback, useEffect, useRef } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { CountdownToast } from "@/components/shared/CountdownToast";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { ConfidenceIndicator } from "@/components/ui/confidence-indicator";
import { ConfidenceEducationPopover } from "@/components/ui/ConfidenceEducationPopover";
import { useBulkApproveThreshold, useConfidenceBands } from "@/hooks/use-system-info";
import {
  ReceiptReviewPanel,
  type ReviewTransaction,
} from "@/components/ReceiptReviewPanel";
import {
  ReconcileProgressPanel,
  type ReconcileProgressState,
} from "@/components/reconciliation/ReconcileProgressPanel";
import {
  summarizeMatchRun,
  type ReconcileRunResult,
} from "@/lib/reconciliation-result";
import {
  ArrowRight,
  CheckCircle,
  FileQuestion,
  Keyboard,
  ChevronLeft,
  ChevronRight,
  Play,
  Loader2,
  Upload,
} from "lucide-react";

// -- Types -------------------------------------------------------------------

type TabKey = "auto_approved" | "review" | "unmatched";

interface MatchesResponse {
  matches: ReviewTransaction[];
  total: number;
  page: number;
  per_page: number;
  queue_counts: Record<string, number>;
}

/**
 * What `POST /api/transactions/reviews/approve-all` answers with: how many it
 * approved, how many it left in Review for being under the floor, and the floor
 * it used. These are the only numbers the toast may report — the page cannot
 * know how many rows the server touched beyond the page it has loaded.
 */
interface ApproveAllResult {
  approved?: number;
  skipped_below_threshold?: number;
  min_confidence?: number;
}

/** A fraction in [0, 1] (or a whole percent) as a whole percent, else null. */
function toPercent(value: number | undefined | null): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const fraction = value > 1 ? value / 100 : value;
  if (fraction <= 0 || fraction > 1) return null;
  return Math.round(fraction * 100);
}

/** The toast after "approve all", built only from what the server reported. */
function describeApproveAll(result: ApproveAllResult | null | undefined): string {
  const approved = typeof result?.approved === "number" ? result.approved : null;
  if (approved === null) {
    return "Approve all finished. The queue below has been re-read from the server.";
  }
  const skipped =
    typeof result?.skipped_below_threshold === "number"
      ? result.skipped_below_threshold
      : null;
  const floor = toPercent(result?.min_confidence);
  const head = `Approved ${approved} match${approved === 1 ? "" : "es"}.`;
  if (skipped === null || skipped === 0) return head;
  return floor === null
    ? `${head} ${skipped} stayed in review, below the server's confidence floor.`
    : `${head} ${skipped} stayed in review, below ${floor}%.`;
}

// -- Status mapping ----------------------------------------------------------

const TAB_CONFIG: Record<
  TabKey,
  {
    statusFilter: string;
    label: string;
    emptyIcon: React.ComponentType<{ className?: string }>;
    emptyTitle: string;
    emptyDescription: string;
  }
> = {
  auto_approved: {
    statusFilter: "AUTO_APPROVED,USER_APPROVED",
    label: "Auto-matched",
    emptyIcon: CheckCircle,
    emptyTitle: "No approved matches",
    emptyDescription:
      "High-confidence matches will appear here after you run Match Receipts.",
  },
  review: {
    statusFilter: "PENDING_REVIEW,NEEDS_REVIEW",
    label: "Needs Review",
    emptyIcon: CheckCircle,
    emptyTitle: "All caught up!",
    emptyDescription:
      "No matches need your review right now. Upload more documents or check your reports.",
  },
  unmatched: {
    statusFilter: "USER_REJECTED,UNMATCHED",
    label: "No Match",
    emptyIcon: FileQuestion,
    emptyTitle: "No unmatched items",
    emptyDescription:
      "All transactions and receipts are matched. Run Match Receipts to find new matches.",
  },
};

// -- Helpers -----------------------------------------------------------------

function formatCurrency(amount: string | null, currency: string): string {
  if (!amount) return "--";
  const num = parseFloat(amount);
  if (isNaN(num)) return amount;
  const prefix = num >= 0 ? "+" : "";
  return `${prefix}${num.toLocaleString("en-CA", {
    style: "currency",
    currency: currency === "CAD" || currency === "USD" ? currency : "CAD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "--";
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
  });
}

function TabBadge({
  count,
  variant,
}: {
  count: number;
  variant: "green" | "amber" | "red";
}) {
  const colorClasses = {
    green:
      "bg-primary/10 text-primary border-primary/20 dark:bg-primary/20 dark:text-primary dark:border-primary/30",
    amber:
      "bg-amber-100 text-amber-800 border-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-800",
    red: "bg-destructive/10 text-destructive border-destructive/20 dark:bg-destructive/20 dark:text-destructive dark:border-destructive/30",
  };

  return (
    <Badge
      className={cn(
        "ml-1.5 h-5 min-w-[20px] justify-center px-1.5 text-[10px] font-bold font-mono",
        colorClasses[variant]
      )}
    >
      {count}
    </Badge>
  );
}

// -- Transaction Row ---------------------------------------------------------

function TransactionRow({
  match,
  isSelected,
  onClick,
  showEducation,
}: {
  match: ReviewTransaction;
  isSelected: boolean;
  onClick: () => void;
  showEducation?: boolean;
}) {
  const txn = match.transaction;
  const doc = match.document;
  const confidencePercent = match.confidence_score * 100;

  const confidenceEl = <ConfidenceIndicator confidence={confidencePercent} />;

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "w-full text-left px-4 py-3.5 border-b border-border/30 transition-all duration-150",
        "hover:bg-primary/[0.03] focus:outline-none focus:bg-primary/[0.03]",
        isSelected &&
          "bg-primary/[0.06] shadow-[inset_3px_0_0_hsl(var(--primary))]"
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs text-muted-foreground/70 font-mono tabular-nums">
              {formatDate(txn.date)}
            </span>
            {showEducation ? (
              <ConfidenceEducationPopover>{confidenceEl}</ConfidenceEducationPopover>
            ) : (
              confidenceEl
            )}
          </div>
          <p className="text-sm font-medium truncate text-foreground">
            {txn.description}
          </p>
          <p className="text-xs text-muted-foreground/60 truncate mt-0.5">
            {doc.vendor ?? "Unknown vendor"} &middot; {doc.filename ?? ""}
          </p>
        </div>
        <span
          className={cn(
            "text-sm font-mono font-semibold tabular-nums whitespace-nowrap",
            txn.amount && parseFloat(txn.amount) >= 0
              ? "text-primary"
              : "text-foreground"
          )}
        >
          {formatCurrency(txn.amount, txn.currency)}
        </span>
      </div>
    </button>
  );
}

// -- Empty State -------------------------------------------------------------

function TabEmptyState({ tab }: { tab: TabKey }) {
  const config = TAB_CONFIG[tab];
  const Icon = config.emptyIcon;
  const isReview = tab === "review";
  return (
    <div className="flex h-[400px] items-center justify-center">
      <div className="text-center space-y-4">
        <div className={cn(
          "mx-auto h-16 w-16 rounded-full flex items-center justify-center",
          isReview ? "bg-success/10" : "bg-primary/5"
        )}>
          <Icon className={cn("h-8 w-8", isReview ? "text-success" : "text-primary/25")} />
        </div>
        <div className="space-y-1">
          <p className={cn("text-sm", isReview ? "font-semibold text-foreground" : "font-medium text-muted-foreground")}>
            {config.emptyTitle}
          </p>
          <p className="text-xs text-muted-foreground/60 max-w-[280px]">
            {config.emptyDescription}
          </p>
        </div>
        {tab === "unmatched" && (
          <Link to="/transactions?status=UNMATCHED">
            <Button variant="outline" size="sm" className="mt-2 gap-1.5">
              Find matches in Transactions
              <ArrowRight className="h-3.5 w-3.5" />
            </Button>
          </Link>
        )}
        {tab === "review" && (
          <div className="flex items-center justify-center gap-2 pt-1">
            <Link to="/transactions?upload=1">
              <Button variant="outline" size="sm" className="gap-1.5">
                <Upload className="h-3.5 w-3.5" />
                Upload Documents
              </Button>
            </Link>
            <Link to="/reports">
              <Button variant="outline" size="sm" className="gap-1.5">
                View Reports
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}

// -- List Skeleton -----------------------------------------------------------

function ListSkeleton() {
  return (
    <div className="space-y-0">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="px-4 py-3.5 border-b border-border/30 space-y-2">
          <div className="flex items-center gap-2">
            <Skeleton className="h-3 w-16 rounded-md" />
            <Skeleton className="h-3 w-3 rounded-full" />
          </div>
          <Skeleton className="h-4 w-3/4 rounded-md" />
          <Skeleton className="h-3 w-1/2 rounded-md" />
        </div>
      ))}
    </div>
  );
}

// -- Main Page ---------------------------------------------------------------

export function ReconciliationReview() {
  const queryClient = useQueryClient();
  // Confidence bands for the legend below, read from the server.
  const bands = useConfidenceBands();
  // Tab + page live in the URL so in-app/browser back-forward restores them.
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const activeTab: TabKey =
    tabParam === "auto_approved" || tabParam === "unmatched" || tabParam === "review"
      ? tabParam
      : "review";
  const page = Math.max(1, Number(searchParams.get("page")) || 1);
  const setActiveTab = useCallback(
    (t: TabKey) => {
      const next = new URLSearchParams(searchParams);
      next.set("tab", t);
      next.delete("page");
      setSearchParams(next);
    },
    [searchParams, setSearchParams],
  );
  const setPage = useCallback(
    (p: number) => {
      const next = new URLSearchParams(searchParams);
      if (p <= 1) next.delete("page");
      else next.set("page", String(p));
      setSearchParams(next);
    },
    [searchParams, setSearchParams],
  );
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [keyboardError, setKeyboardError] = useState<string | null>(null);
  const [isStartingMatch, setIsStartingMatch] = useState(false);
  // Match Receipts runs the same job /transactions runs, and a whole-account
  // run is long enough that a button reading only "Matching..." tells the user
  // nothing. The progress endpoint carries phase, percent, matches-so-far and a
  // log the whole time, so render that instead.
  const [matchProgress, setMatchProgress] = useState<ReconcileProgressState | null>(null);
  const [matchLogs, setMatchLogs] = useState<string[]>([]);
  const [matchSaving, setMatchSaving] = useState(false);
  const [showShortcutOverlay, setShowShortcutOverlay] = useState(false);
  const [bulkApproveOpen, setBulkApproveOpen] = useState(false);
  const [undoAction, setUndoAction] = useState<{
    matchId: number;
    action: "approved" | "rejected";
    snapshot: [readonly unknown[], MatchesResponse | undefined][];
  } | null>(null);
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  const [isDesktop, setIsDesktop] = useState(
    typeof window !== "undefined" ? window.innerWidth >= 1024 : true
  );

  useEffect(() => {
    function handleResize() { setIsDesktop(window.innerWidth >= 1024); }
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  // Show undo toast (CountdownToast handles its own timer)
  const showUndoToast = useCallback(
    (matchId: number, action: "approved" | "rejected", snapshot: [readonly unknown[], MatchesResponse | undefined][]) => {
      setUndoAction({ matchId, action, snapshot });
    },
    []
  );

  const statusFilter = TAB_CONFIG[activeTab].statusFilter;

  // Fetch matches for the active tab
  const { data, isLoading, isError, refetch } = useQuery<MatchesResponse>({
    queryKey: ["review-matches", statusFilter, page],
    queryFn: () =>
      api.get(
        `/api/reconciliation/matches?status=${statusFilter}&page=${page}&per_page=50`
      ),
  });

  const matches = data?.matches ?? [];
  const queueCounts = data?.queue_counts ?? {};
  const selectedMatch = matches[selectedIndex] ?? null;

  // Reset selection when tab changes (page reset happens in setActiveTab)
  useEffect(() => {
    setSelectedIndex(0);
  }, [activeTab]);

  // Clamp selection index when the list shrinks (e.g. after approve/reject removes
  // the current item). Keep the same index so the next item that slid into position
  // is now selected (auto-advance). If the removed item was the last one, clamp to
  // the new last item instead of resetting to 0.
  useEffect(() => {
    if (matches.length === 0) return;
    if (selectedIndex >= matches.length) {
      setSelectedIndex(matches.length - 1);
    }
  }, [matches.length, selectedIndex]);

  const scrollContainerRef = useRef<HTMLDivElement | null>(null);

  // After action, advance to next item
  const handleAfterAction = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["review-matches"] });
    queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
  }, [queryClient]);

  // Optimistically remove a match from the review-matches cache and update
  // queue_counts. Returns a snapshot for rollback on error.
  const optimisticRemoveMatch = useCallback(
    (matchId: number, newStatus: string) => {
      const prevData = queryClient.getQueriesData<MatchesResponse>({
        queryKey: ["review-matches"],
      });
      queryClient.setQueriesData<MatchesResponse>(
        { queryKey: ["review-matches"] },
        (old) => {
          if (!old) return old;
          const removed = old.matches.find((m) => m.id === matchId);
          const filtered = old.matches.filter((m) => m.id !== matchId);
          const newCounts = { ...old.queue_counts };
          if (removed) {
            const srcStatus = removed.status;
            if (newCounts[srcStatus] !== undefined) {
              newCounts[srcStatus] = Math.max(0, newCounts[srcStatus] - 1);
            }
            newCounts[newStatus] = (newCounts[newStatus] ?? 0) + 1;
          }
          return {
            ...old,
            matches: filtered,
            total: Math.max(0, old.total - (removed ? 1 : 0)),
            queue_counts: newCounts,
          };
        }
      );
      return prevData;
    },
    [queryClient]
  );

  // Rollback optimistic update using a snapshot
  const rollbackOptimistic = useCallback(
    (prevData: [readonly unknown[], MatchesResponse | undefined][]) => {
      prevData.forEach(([key, data]) => queryClient.setQueryData(key, data));
    },
    [queryClient]
  );

  const handleUndo = useCallback(() => {
    if (!undoAction) return;
    // Rollback optimistic update
    rollbackOptimistic(undoAction.snapshot);
    // Revert on server
    const revertEndpoint = `/api/reconciliation/matches/${undoAction.matchId}/revert`;
    api.patch(revertEndpoint).then(() => {
      toast.success("Undone — the match is back in the queue.");
      handleAfterAction();
    }).catch(() => {
      setKeyboardError("Undo failed — the original action was kept. Please try again.");
      toast.error("Undo failed — the original action was kept.");
      handleAfterAction();
    });
    setUndoAction(null);
  }, [undoAction, rollbackOptimistic, handleAfterAction]);

  // -- Keyboard shortcuts ---------------------------------------------------

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
        e.preventDefault();
        setShowShortcutOverlay((prev) => !prev);
        return;
      }
      if (e.key === "Escape" && showShortcutOverlay) {
        setShowShortcutOverlay(false);
        return;
      }

      switch (e.key.toLowerCase()) {
        case "a":
          if (selectedMatch) {
            const approvableStatuses = [
              "AUTO_APPROVED",
              "NEEDS_REVIEW",
              "PENDING_REVIEW",
            ];
            if (approvableStatuses.includes(selectedMatch.status)) {
              const matchId = selectedMatch.id;
              const snapshot = optimisticRemoveMatch(matchId, "USER_APPROVED");
              api
                .patch(
                  `/api/reconciliation/matches/${matchId}/approve`
                )
                .then(() => {
                  setKeyboardError(null);
                  showUndoToast(matchId, "approved", snapshot);
                  handleAfterAction();
                })
                .catch(() => {
                  rollbackOptimistic(snapshot);
                  setKeyboardError("Action failed. Please try again.");
                  toast.error("Action failed. Please try again.");
                });
            }
          }
          break;
        case "r":
          if (selectedMatch) {
            const rejectableStatuses = [
              "AUTO_APPROVED",
              "NEEDS_REVIEW",
              "PENDING_REVIEW",
            ];
            if (rejectableStatuses.includes(selectedMatch.status)) {
              const matchId = selectedMatch.id;
              const snapshot = optimisticRemoveMatch(matchId, "USER_REJECTED");
              api
                .patch(
                  `/api/reconciliation/matches/${matchId}/reject`
                )
                .then(() => {
                  setKeyboardError(null);
                  showUndoToast(matchId, "rejected", snapshot);
                  handleAfterAction();
                })
                .catch(() => {
                  rollbackOptimistic(snapshot);
                  setKeyboardError("Action failed. Please try again.");
                  toast.error("Action failed. Please try again.");
                });
            }
          }
          break;
        case "s":
          if (matches.length > 0) {
            setSelectedIndex((prev) =>
              prev < matches.length - 1 ? prev + 1 : prev
            );
          }
          break;
        case "u":
          if (undoAction) {
            handleUndo();
          }
          break;
        case "arrowdown":
          e.preventDefault();
          if (matches.length > 0) {
            setSelectedIndex((prev) =>
              prev < matches.length - 1 ? prev + 1 : prev
            );
          }
          break;
        case "arrowup":
          e.preventDefault();
          if (matches.length > 0) {
            setSelectedIndex((prev) => (prev > 0 ? prev - 1 : prev));
          }
          break;
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectedMatch, matches.length, handleAfterAction, optimisticRemoveMatch, rollbackOptimistic, showShortcutOverlay, undoAction, handleUndo]);

  // Count helpers — use data.total for the active tab (guaranteed consistent
  // with the matches list), queue_counts for inactive tabs (best-effort).
  const queueAutoApproved =
    (queueCounts["AUTO_APPROVED"] ?? 0) + (queueCounts["USER_APPROVED"] ?? 0);
  const queueReview =
    (queueCounts["NEEDS_REVIEW"] ?? 0) + (queueCounts["PENDING_REVIEW"] ?? 0);
  const queueUnmatched =
    (queueCounts["UNMATCHED"] ?? 0) + (queueCounts["USER_REJECTED"] ?? 0);

  const autoApprovedCount = activeTab === "auto_approved" ? (data?.total ?? queueAutoApproved) : queueAutoApproved;
  const reviewCount = activeTab === "review" ? (data?.total ?? queueReview) : queueReview;
  const unmatchedCount = activeTab === "unmatched" ? (data?.total ?? queueUnmatched) : queueUnmatched;

  // What "Approve All" will actually do, said before it is pressed.
  //
  // The floor is the server's (`/health` → reconciliation), not a number from
  // this bundle, and the split is counted over the review rows this page has
  // loaded. When the queue is longer than the loaded page the sentence says so
  // rather than passing a page count off as the whole queue.
  const bulkFloor = useBulkApproveThreshold();
  const loadedReview = activeTab === "review" ? matches : [];
  const eligible = bulkFloor === null
    ? null
    : loadedReview.filter((m) => m.confidence_score * 100 >= bulkFloor).length;
  const below = eligible === null ? null : loadedReview.length - eligible;
  const wholeQueueLoaded = loadedReview.length >= reviewCount;

  let bulkApproveDescription: string;
  if (bulkFloor === null) {
    bulkApproveDescription =
      "This approves every pending match at or above the confidence floor this server is configured with; anything below it stays in review. The server has not reported that floor, so no percentage is shown here.";
  } else if (wholeQueueLoaded) {
    bulkApproveDescription =
      `Approves the ${eligible} pending match${eligible === 1 ? "" : "es"} at or above ` +
      `${bulkFloor}% confidence; ${below} below that stay${below === 1 ? "s" : ""} in review.`;
  } else {
    bulkApproveDescription =
      `Approves every pending match at or above ${bulkFloor}% confidence, across all ` +
      `${reviewCount} in the queue; anything below that stays in review. On this page, ` +
      `${eligible} of ${loadedReview.length} qualify.`;
  }

  if (isError) {
    return <ErrorDisplay error="Could not load matches" context="matches" onRetry={refetch} />;
  }

  return (
    <div className="space-y-6 pt-4">
      {/* Visually-hidden announcer for screen readers */}
      <div aria-live="assertive" role="status" className="sr-only">
        {undoAction ? `Match ${undoAction.action}` : ""}
      </div>

      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-3xl font-bold tracking-tight text-foreground">
            Review
          </h2>
          <p className="text-muted-foreground mt-1">
            Review and approve matched transactions and receipts.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button
            size="sm"
            className="gap-1.5 bg-primary hover:bg-primary/90 shadow-warm-sm"
            disabled={isStartingMatch}
            onClick={async () => {
              setIsStartingMatch(true);
              setMatchSaving(false);
              setMatchProgress({ status: "running", phase: "Starting...", percent: 0 });
              setMatchLogs(["Matching receipts..."]);
              try {
                await api.post("/api/reconciliation/start");
                setMatchLogs((prev) => [
                  ...prev,
                  "Matching request sent, waiting for progress...",
                ]);
                // The run is async server-side and how long it takes depends on
                // the account and the extraction endpoint, so poll until it
                // actually finishes rather than giving up on a timer, and
                // surface every phase/percent the endpoint reports.
                const deadline = Date.now() + 45 * 60 * 1000;
                let lastPhase = "";
                let idleCount = 0;
                let finished = false;
                // The run summary, kept for the completion toast below.
                let runResult: ReconcileRunResult | undefined;
                while (!finished && Date.now() < deadline) {
                  await new Promise((r) => setTimeout(r, 2000));
                  try {
                    const p = await api.get<ReconcileProgressState & { status?: string }>(
                      "/api/reconciliation/progress"
                    );
                    if (!p.status) break;
                    if (p.status === "running") {
                      idleCount = 0;
                      setMatchProgress(p);
                      const phaseStr = `${p.phase ?? ""} ${p.detail ?? ""}`.trim();
                      if (phaseStr && phaseStr !== lastPhase) {
                        lastPhase = phaseStr;
                        const pct = p.percent != null ? ` (${p.percent}%)` : "";
                        setMatchLogs((prev) => [...prev, `${phaseStr}${pct}`]);
                      }
                    } else if (p.status === "complete") {
                      if (p.logs?.length) {
                        setMatchLogs((prev) => [
                          ...prev,
                          ...p.logs!.filter((l) => !prev.includes(l)),
                        ]);
                      }
                      setMatchProgress({ ...p, percent: 100 });
                      runResult = p.result;
                      setMatchLogs((prev) => [
                        ...prev,
                        `Matching complete. ${summarizeMatchRun(p.result)}.`,
                      ]);
                      finished = true;
                    } else if (p.status === "error") {
                      setMatchProgress({ ...p });
                      setMatchLogs((prev) => [
                        ...prev,
                        `ERROR: ${p.detail || "Matching failed"}`,
                      ]);
                      finished = true;
                    } else {
                      // "idle" right after start can mean the run has not been
                      // registered yet; only treat a run of idles as the end.
                      idleCount++;
                      if (idleCount >= 3) {
                        setMatchProgress({ status: "complete", percent: 100 });
                        setMatchLogs((prev) => [...prev, "Matching finished."]);
                        finished = true;
                      }
                    }
                  } catch {
                    break;
                  }
                }
                setMatchSaving(true);
                setMatchLogs((prev) => [...prev, "Refreshing the review queue..."]);
                await Promise.all([
                  queryClient.invalidateQueries({ queryKey: ["review-matches"] }),
                  queryClient.invalidateQueries({ queryKey: ["review-match-counts"] }),
                ]);
                setMatchLogs((prev) => [...prev, "Review queue up to date."]);
                // The panel scrolls away; a toast is the part that persists.
                // Read the counts straight out of the cache the refetch just
                // filled — the `queueCounts` captured at click time is stale by
                // now, and a re-render is not guaranteed to have landed yet.
                const fresh = queryClient.getQueryData<MatchesResponse>([
                  "review-matches",
                  statusFilter,
                  page,
                ]);
                const freshCounts = fresh?.queue_counts ?? {};
                const waiting =
                  (freshCounts["PENDING_REVIEW"] ?? 0) +
                  (freshCounts["NEEDS_REVIEW"] ?? 0);
                // Two different facts, never to be conflated: what this run
                // created, and how big the queue is now. A re-run that only
                // re-scored pairs it already had says "0 new matches".
                const runSummary = summarizeMatchRun(runResult);
                const detail =
                  waiting > 0
                    ? `${runSummary}; ${waiting} waiting for review.`
                    : `${runSummary}.`;
                if ((runResult?.matched ?? 0) > 0 || waiting > 0) {
                  toast.success(`Matching complete — ${detail}`);
                } else {
                  toast.info(`Matching complete — ${detail}`);
                }
              } catch {
                setKeyboardError("Could not start matching. Please try again.");
                toast.error("Could not start matching. Please try again.");
                setMatchProgress({ status: "error", percent: 0 });
                setMatchLogs((prev) => [...prev, "ERROR: could not start matching."]);
              } finally {
                setMatchSaving(false);
                setIsStartingMatch(false);
              }
            }}
          >
            {isStartingMatch ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            {isStartingMatch ? "Matching..." : "Match Receipts"}
          </Button>
        <button
          onClick={() => setShowShortcutOverlay(true)}
          className="hidden md:flex items-center gap-2 rounded-xl bg-muted/50 border border-border/40 px-3 py-2 hover:bg-muted/80 transition-colors"
          title="Keyboard shortcuts (press ? for full list)"
        >
          <Keyboard className="h-3.5 w-3.5 text-muted-foreground/60" />
          <span className="text-xs text-muted-foreground/70">
            <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
              A
            </kbd>{" "}
            Approve{" "}
            <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
              R
            </kbd>{" "}
            Reject{" "}
            <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
              S
            </kbd>{" "}
            Skip{" "}
            <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
              U
            </kbd>{" "}
            Undo{" "}
            <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
              ?
            </kbd>{" "}
            Help
          </span>
        </button>
        </div>
      </div>

      {/* Live progress for the Match Receipts run above - same panel the
          Transactions page uses, reading the same progress endpoint. */}
      <ReconcileProgressPanel
        progress={matchProgress}
        logs={matchLogs}
        saving={matchSaving}
        onDismiss={() => {
          setMatchProgress(null);
          setMatchLogs([]);
        }}
      />

      {/* How confidence works legend. Every band comes from the server's
          `/health` reconciliation block, because the engine's thresholds are
          env-tunable per instance; with no answer yet the legend describes the
          bands without claiming a number. */}
      <details className="rounded-xl border border-border/40 bg-card p-4 shadow-warm-sm">
        <summary className="text-xs font-semibold uppercase tracking-wider text-muted-foreground cursor-pointer select-none">
          How confidence works
        </summary>
        <div className="mt-3 space-y-2">
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-0.5">
              {[true, true, true, true].map((_, i) => (
                <div key={i} className="h-2 w-2 rounded-full bg-success" />
              ))}
            </div>
            <span className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">HIGH</span>
              {bands ? ` (${bands.high}%+)` : ""} — Approved without review
            </span>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-0.5">
              {[true, true, true, false].map((filled, i) => (
                <div key={i} className={`h-2 w-2 rounded-full ${filled ? "bg-amber-500" : "bg-muted-foreground/20"}`} />
              ))}
            </div>
            <span className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">MEDIUM</span>
              {bands ? ` (${bands.medium}–${bands.high - 1}%)` : ""} — Needs your review
            </span>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-0.5">
              {[true, true, false, false].map((filled, i) => (
                <div key={i} className={`h-2 w-2 rounded-full ${filled ? "bg-red-500" : "bg-muted-foreground/20"}`} />
              ))}
            </div>
            <span className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">LOW</span>
              {bands ? ` (<${bands.medium}%)` : ""} — May need manual matching
            </span>
          </div>
          <p className="text-[11px] text-muted-foreground/80 pt-1">
            {bands
              ? "These thresholds are configured on the server running this instance."
              : "This instance has not reported its thresholds, so no percentages are shown."}
          </p>
        </div>
      </details>

      {/* Undo toast */}
      {undoAction && (
        <CountdownToast
          message={`Match ${undoAction.action}`}
          duration={12000}
          onAction={handleUndo}
          onExpire={() => setUndoAction(null)}
        />
      )}

      {/* Tabs */}
      <Tabs
        value={activeTab}
        onValueChange={(v) => setActiveTab(v as TabKey)}
      >
        <TabsList className="bg-muted/50 border border-border/40 rounded-xl p-1">
          <TabsTrigger
            value="auto_approved"
            className="rounded-lg data-[state=active]:bg-card data-[state=active]:shadow-warm-sm data-[state=active]:text-foreground"
          >
            Auto-matched
            <TabBadge count={autoApprovedCount} variant="green" />
          </TabsTrigger>
          <TabsTrigger
            value="review"
            className="rounded-lg data-[state=active]:bg-card data-[state=active]:shadow-warm-sm data-[state=active]:text-foreground"
          >
            Needs Review
            <TabBadge count={reviewCount} variant="amber" />
          </TabsTrigger>
          <TabsTrigger
            value="unmatched"
            className="rounded-lg data-[state=active]:bg-card data-[state=active]:shadow-warm-sm data-[state=active]:text-foreground"
          >
            No Match
            <TabBadge count={unmatchedCount} variant="red" />
          </TabsTrigger>
        </TabsList>

        {/* Bulk approve action (only on review tab) */}
        {activeTab === "review" && reviewCount > 0 && (
          <div className="mt-3 flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5 text-xs border-primary/30 text-primary hover:bg-primary/5"
              disabled={isStartingMatch}
              onClick={() => setBulkApproveOpen(true)}
            >
              <CheckCircle className="h-3.5 w-3.5" />
              Approve All High-Confidence
            </Button>
          </div>
        )}

        {/* Tab content: shared layout with split pane */}
        {(["auto_approved", "review", "unmatched"] as TabKey[]).map((tab) => (
          <TabsContent key={tab} value={tab} className="mt-4">
            {keyboardError && (
              <p role="alert" aria-live="polite" className="mb-3 text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
                {keyboardError}
              </p>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
              {/* Left panel: transaction list (60%) */}
              <Card className="lg:col-span-3 overflow-hidden shadow-warm rounded-xl border-border/60">
                <CardContent className="p-0">
                  {isLoading ? (
                    <ListSkeleton />
                  ) : matches.length === 0 ? (
                    <TabEmptyState tab={tab} />
                  ) : (
                    <div ref={scrollContainerRef} className="max-h-[calc(100vh-280px)] overflow-y-auto">
                      {matches.map((match, index) => (
                        <TransactionRow
                          key={match.id}
                          match={match}
                          isSelected={index === selectedIndex}
                          onClick={() => {
                            setSelectedIndex(index);
                            if (!isDesktop) setMobileDetailOpen(true);
                          }}
                          showEducation={index === 0}
                        />
                      ))}

                      {/* Pagination / count */}
                      {data && (
                        <div className="flex items-center justify-between px-4 py-3 border-t border-border/30 bg-muted/20">
                          <span className="text-xs font-mono text-muted-foreground">
                            Showing{" "}
                            <span className="font-semibold text-foreground">
                              {matches.length > 0
                                ? `${(page - 1) * data.per_page + 1}-${(page - 1) * data.per_page + matches.length}`
                                : "0"}
                            </span>{" "}
                            of{" "}
                            <span className="font-semibold text-foreground">
                              {data.total}
                            </span>
                          </span>
                          {data.total > data.per_page && (
                            <div className="flex gap-1">
                              <button
                                className="h-7 w-7 rounded-full flex items-center justify-center text-muted-foreground hover:bg-primary/5 hover:text-primary disabled:opacity-40 transition-colors"
                                disabled={page === 1}
                                onClick={() => setPage(Math.max(1, page - 1))}
                              >
                                <ChevronLeft className="h-3.5 w-3.5" />
                              </button>
                              <button
                                className="h-7 w-7 rounded-full flex items-center justify-center text-muted-foreground hover:bg-primary/5 hover:text-primary disabled:opacity-40 transition-colors"
                                disabled={page * data.per_page >= data.total}
                                onClick={() => setPage(page + 1)}
                              >
                                <ChevronRight className="h-3.5 w-3.5" />
                              </button>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </CardContent>
              </Card>

              {/* Right panel: review detail (40%) — desktop only */}
              {isDesktop && (
              <Card className="lg:col-span-2 overflow-hidden shadow-warm rounded-xl border-border/60">
                <CardContent className="p-0 h-[calc(100vh-280px)]">
                  <ReceiptReviewPanel
                    transaction={selectedMatch}
                    onApprove={handleAfterAction}
                    onReject={handleAfterAction}
                    onUnmatch={handleAfterAction}
                    showActions={tab !== "unmatched"}
                    activeTab={activeTab}
                  />
                </CardContent>
              </Card>
              )}
            </div>
          </TabsContent>
        ))}
      </Tabs>

      {/* Mobile bottom-sheet detail panel */}
      {!isDesktop && (
        <Sheet open={mobileDetailOpen} onOpenChange={setMobileDetailOpen}>
          <SheetContent side="bottom" className="h-[85vh] overflow-y-auto rounded-t-2xl p-0">
            <SheetHeader className="px-4 pt-4 pb-2">
              <SheetTitle className="text-base">
                {selectedMatch?.transaction?.description ?? "Transaction Detail"}
              </SheetTitle>
              <SheetDescription className="sr-only">
                Review transaction match details
              </SheetDescription>
            </SheetHeader>
            <div className="h-full">
              <ReceiptReviewPanel
                transaction={selectedMatch}
                onApprove={() => { handleAfterAction(); setMobileDetailOpen(false); }}
                onReject={() => { handleAfterAction(); setMobileDetailOpen(false); }}
                onUnmatch={() => { handleAfterAction(); setMobileDetailOpen(false); }}
                showActions={activeTab !== "unmatched"}
                activeTab={activeTab}
              />
            </div>
          </SheetContent>
        </Sheet>
      )}

      {/* Keyboard shortcut help overlay */}
      {showShortcutOverlay && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
          onClick={() => setShowShortcutOverlay(false)}
        >
          <div
            className="bg-card border border-border/60 rounded-2xl shadow-warm-lg p-8 max-w-md w-full mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-lg font-bold text-foreground">Keyboard Shortcuts</h3>
              <button
                onClick={() => setShowShortcutOverlay(false)}
                className="text-muted-foreground hover:text-foreground text-sm"
              >
                <kbd className="rounded-md border border-border bg-muted px-1.5 py-0.5 text-xs font-mono">Esc</kbd>
              </button>
            </div>
            <div className="space-y-5">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">Review Actions</p>
                <div className="space-y-2">
                  {[
                    { key: "A", desc: "Approve selected match" },
                    { key: "R", desc: "Reject selected match" },
                    { key: "S", desc: "Skip to next match" },
                    { key: "U", desc: "Undo last action" },
                  ].map((s) => (
                    <div key={s.key} className="flex items-center justify-between">
                      <span className="text-sm text-foreground">{s.desc}</span>
                      <kbd className="rounded-md border border-border bg-muted px-2 py-1 text-xs font-mono font-semibold text-foreground shadow-sm">
                        {s.key}
                      </kbd>
                    </div>
                  ))}
                </div>
              </div>
              <div>
                <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">Navigation</p>
                <div className="space-y-2">
                  {[
                    { key: "\u2191", desc: "Previous match" },
                    { key: "\u2193", desc: "Next match" },
                    { key: "?", desc: "Toggle this help" },
                  ].map((s) => (
                    <div key={s.key} className="flex items-center justify-between">
                      <span className="text-sm text-foreground">{s.desc}</span>
                      <kbd className="rounded-md border border-border bg-muted px-2 py-1 text-xs font-mono font-semibold text-foreground shadow-sm">
                        {s.key}
                      </kbd>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Bulk approve confirm dialog. The description states the rule the
          server actually applies, with the floor read from `/health`; the
          counts are over the queue this page has loaded, and the toast
          afterwards reports what the server says it did — never a number
          computed here. */}
      <ConfirmDialog
        open={bulkApproveOpen}
        onOpenChange={setBulkApproveOpen}
        title="Approve all high-confidence matches?"
        description={bulkApproveDescription}
        confirmLabel="Approve All"
        variant="default"
        onConfirm={async () => {
          try {
            const result = await api.post<ApproveAllResult>(
              "/api/transactions/reviews/approve-all",
            );
            toast.success(describeApproveAll(result));
            queryClient.invalidateQueries({ queryKey: ["review-matches"] });
            queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
          } catch {
            setKeyboardError("Bulk approve failed. Please try again.");
            toast.error("Bulk approve failed. Please try again.");
          } finally {
            setBulkApproveOpen(false);
          }
        }}
      />
    </div>
  );
}
