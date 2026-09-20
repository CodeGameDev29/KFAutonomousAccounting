import { useState, useCallback, useEffect, useRef, useMemo } from "react";
import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { useSearchParams, useNavigate } from "react-router-dom";
import { api, ApiError } from "@/lib/api";
import { useCategoryTaxonomy } from "@/hooks/use-category-taxonomy";
import { useAccountDisplayNames } from "@/hooks/use-account-names";
import { withParams } from "@/lib/url-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  TransactionTable,
  type Transaction,
  type SortState,
} from "@/components/transactions/TransactionTable";
import {
  TransactionFilters,
  type TransactionFiltersState,
  type DateRange,
} from "@/components/transactions/TransactionFilters";
import { ReceiptSlideOver } from "@/components/transactions/ReceiptSlideOver";
import { ReceiptSelectorDialog } from "@/components/transactions/ReceiptSelectorDialog";
import { ReceiptViewerModal } from "@/components/transactions/ReceiptViewerModal";
import { UploadZone } from "@/components/transactions/UploadZone";
import { ProcessingQueuePanel } from "@/components/processing/ProcessingQueuePanel";
import { Pagination } from "@/components/transactions/Pagination";
import { NotePanel } from "@/components/transactions/NotePanel";
import { TransactionLinkDialog } from "@/components/transactions/TransactionLinkDialog";
import { ChainBreadcrumb } from "@/components/transactions/ChainBreadcrumb";
import { ProofStatusBadge } from "@/components/transactions/ProofStatusBadge";
import { TransactionsPagePrompt } from "@/components/transactions/TransactionsPagePrompt";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import type { ChainBreadcrumbEntry } from "@/types/transaction-links";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Upload,
  FileText,
  RefreshCw,
  Loader2,
  CheckCircle,
  AlertTriangle,
  X,
  Trash2,
  ExternalLink,
  ArrowRight,
  ArrowLeftRight,
  ShoppingCart,
  Sparkles,
  TrendingUp,
  Keyboard,
  Search,
} from "lucide-react";
import { AmazonImportWalkthrough } from "@/components/AmazonImportWalkthrough";
import { WiseTokenWalkthrough } from "@/components/WiseTokenWalkthrough";
import { Input } from "@/components/ui/input";
import { ProgressLogPanel } from "@/components/shared/ProgressLogPanel";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { cn } from "@/lib/utils";
import { API_URL } from "@/lib/constants";
import { parseSearchId, amountMatchesSearch, fuzzyTextMatch } from "@/lib/search";
import {
  summarizeMatchRun,
  type ReconcileRunResult as ReconcileResult,
} from "@/lib/reconciliation-result";
import { useAuthStore } from "@/stores/auth-store";
import { useUploadIntentStore } from "@/stores/upload-intent-store";
import { useAddProof } from "@/hooks/use-transaction-proofs";
import { toast } from "sonner";

interface TransactionsResponse {
  transactions: Transaction[];
  total: number;
  page: number;
  per_page: number;
}


interface ReconcileStartResponse {
  status: "started" | "already_running";
}

/** Say out loud that a match run finished, and with what.
 *
 * The progress banner disappears when a run ends, so without this a user
 * watching "Match This Statement" is left with no completion signal and no
 * count to read.
 *
 * The count is *new* matches. A second run over the same statement re-scores
 * the flagged pairs it left in the pool and stores nothing, so it says
 * "0 new matches, 1 re-scored" rather than reporting a match it never made. */
function announceReconciliationComplete(result?: ReconcileResult | null) {
  const summary = summarizeMatchRun(result);
  if ((result?.matched ?? 0) > 0) {
    toast.success(`Matching complete — ${summary}.`);
  } else {
    toast.info(`Matching complete — ${summary}.`);
  }
}

interface ReconcileProgress {
  status: "idle" | "running" | "complete" | "error";
  phase?: string;
  detail?: string;
  percent?: number;
  matches_so_far?: number;
  result?: ReconcileResult;
  logs?: string[];
  /** Work units in the phase now running, and how many are done. */
  pairs_total?: number;
  pairs_done?: number;
  /** Engine LLM calls so far this run. */
  llm_calls_done?: number;
  /** Rolling estimate for the phase now running; null until it is trustworthy. */
  eta_seconds?: number | null;
}

/**
 * "about 4 min left" / "about 40s left" for the matching countdown.
 * Deliberately coarse: the estimate is a rolling average over the last few
 * completions, so a to-the-second countdown would claim a precision it does
 * not have. Mirrors the same function in ReconcileProgressPanel, which is what
 * the Review page renders.
 */
function formatReconEta(seconds: number): string {
  if (seconds < 45) return `about ${Math.max(5, Math.round(seconds / 5) * 5)}s left`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `about ${hours}h ${rest}m left` : `about ${hours}h left`;
}

interface ReconSettings {
  max_date_variance_days: number;
  options: number[];
}

interface SourceFileInfo {
  source_file: string;
  account: string;
  transaction_count: number;
  min_date: string | null;
  max_date: string | null;
}

interface UploadedDocument {
  id: number;
  filename: string;
  vendor: string | null;
  date: string | null;
  total: string | null;
  currency: string | null;
  confidence: string | null;
  status: string;
  created_at: string | null;
  // First matched transaction id (smallest). Use for jump-to-row.
  matched_transaction_id: number | null;
  // Total count of distinct transactions this doc covers. >1 means
  // ONE_TO_MANY split-coverage.
  matched_transaction_count?: number;
  matched_source_file: string | null;
  // All matched transaction ids, for multi-highlight UI.
  matched_transaction_ids?: number[];
}

interface WiseProfile {
  id: number;
  type: string;
  name: string;
  balances: { currency: string; amount: number }[];
}

/**
 * Detect Wise per-month source_file keys: wise_{CURRENCY}_{YYYYMM}
 * Examples: "wise_USD_202601", "wise_CAD_202603"
 */
const WISE_MONTHLY_REGEX = /^wise_([A-Z]{3})_(\d{4})(\d{2})$/;
const PAYPAL_MONTHLY_REGEX = /^paypal_([A-Z]{3})_(\d{4})(\d{2})$/;

const MONTH_SHORT_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatDateRange(minDate: string | null, maxDate: string | null): string {
  if (!minDate || !maxDate) return "";
  // Bare YYYY-MM-DD strings parse as UTC midnight; pin them to local midnight.
  const min = new Date(`${minDate.slice(0, 10)}T00:00:00`);
  const max = new Date(`${maxDate.slice(0, 10)}T00:00:00`);
  const minMonth = min.toLocaleDateString("en-CA", { month: "short" });
  const maxMonth = max.toLocaleDateString("en-CA", { month: "short" });
  const year = min.getFullYear();
  if (minMonth === maxMonth) return `${minMonth} ${year}`;
  return `${minMonth}\u2013${maxMonth} ${year}`;
}

function formatTabLabel(
  sf: SourceFileInfo,
  accountNames: Record<string, string>
): string {
  // Check for Wise per-month source file
  const wiseMatch = sf.source_file.match(WISE_MONTHLY_REGEX);
  if (wiseMatch) {
    const currency = wiseMatch[1];
    const monthIdx = parseInt(wiseMatch[3], 10) - 1;
    const monthName = MONTH_SHORT_NAMES[monthIdx] ?? wiseMatch[3];
    return `Wise ${currency} \u00B7 ${monthName} ${wiseMatch[2]}`;
  }

  // Check for PayPal per-month source file
  const ppMatch = sf.source_file.match(PAYPAL_MONTHLY_REGEX);
  if (ppMatch) {
    const currency = ppMatch[1];
    const monthIdx = parseInt(ppMatch[3], 10) - 1;
    const monthName = MONTH_SHORT_NAMES[monthIdx] ?? ppMatch[3];
    return `PayPal ${currency} \u00B7 ${monthName} ${ppMatch[2]}`;
  }

  // Fallback: an uploaded chequing/credit-card statement. The display name
  // comes from config/accounts.yaml via the server, not from the bundle.
  const name = accountNames[sf.account] || sf.account;
  const dateRange = formatDateRange(sf.min_date, sf.max_date);
  return dateRange ? `${name} \u00B7 ${dateRange}` : name;
}

export function Transactions() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  // Account key -> human name, from config/accounts.yaml on the server.
  const accountNames = useAccountDisplayNames();

  // Table state lives in the URL so browser/in-app back-forward restores it
  // (filters, page, sort survive navigation as history entries).
  const [searchParams, setSearchParams] = useSearchParams();
  const filters: TransactionFiltersState = useMemo(
    () => ({
      account: searchParams.get("account") ?? "",
      status: searchParams.get("status") ?? "",
      month: searchParams.get("fmonth") ?? "",
      search: searchParams.get("q") ?? "",
      min_amount: searchParams.get("min") ?? "",
      max_amount: searchParams.get("max") ?? "",
    }),
    [searchParams],
  );
  const page = Math.max(1, Number(searchParams.get("page")) || 1);
  const perPage = 50;
  const sortParam = searchParams.get("sort");
  const sort: SortState = useMemo(() => {
    if (!sortParam) return { column: null, direction: "asc" };
    const [column, direction] = sortParam.split(":");
    return {
      column: (column || null) as SortState["column"],
      direction: direction === "desc" ? "desc" : "asc",
    };
  }, [sortParam]);
  // Pagination clicks are navigational — they push history entries.
  const setPage = useCallback(
    (p: number) => {
      setSearchParams((prev) =>
        withParams(prev, { page: p <= 1 ? null : String(p) }),
      );
    },
    [setSearchParams],
  );
  const [selectedTransaction, setSelectedTransaction] =
    useState<Transaction | null>(null);
  const [slideOverOpen, setSlideOverOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  // Which promise the open zone is making. "Upload Receipts & Invoices" on this
  // page is the user stating what these files are, and that word is taken at
  // face value. The sidebar's "Upload Documents" is not: it is a folder of what
  // the business produced this month, so that one sniffs each PDF and routes a
  // bank statement to the statement pipeline instead of extracting a month of
  // transactions as if it were one receipt.
  const [uploadMode, setUploadMode] = useState<"receipt" | "auto">("receipt");
  const [statementUploadOpen, setStatementUploadOpen] = useState(false);

  // Opening the zone from somewhere else on the page — or from another page
  // entirely. Every caller funnels through here so the zone is opened, the
  // other zone is closed, and the thing is scrolled into view: a zone that
  // opens far above the viewport, which is where it lands from the bottom of a
  // long table, reads as a dead button just as surely as one that never opens.
  const openUploadZone = useCallback((mode: "receipt" | "auto") => {
    setUploadMode(mode);
    setUploadOpen(true);
    setStatementUploadOpen(false);
    // After React has committed the zone, not before — it does not exist in
    // the DOM until then.
    requestAnimationFrame(() => {
      document
        .getElementById("upload-zone")
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }, []);

  // The sidebar's "Upload Documents", and anything else that asks for the zone
  // without being able to reach into this page. Keyed on the request itself, so
  // it fires on every click — a mount-only effect would fire once per page load
  // and do nothing on any later click from inside the app.
  const pendingUpload = useUploadIntentStore((s) => s.pending);
  const consumeUpload = useUploadIntentStore((s) => s.consumeUpload);
  useEffect(() => {
    if (!pendingUpload) return;
    openUploadZone(pendingUpload);
    // Consumed, so landing on this page again later by ordinary navigation
    // does not re-open a zone nobody asked for.
    consumeUpload();
  }, [pendingUpload, consumeUpload, openUploadZone]);

  // ?upload=1 — still a real deep link (the onboarding completion screen and
  // the reconciliation review both send users here that way). Keyed on the
  // param so it works on a client-side navigation too, not just a hard load.
  const uploadParam = searchParams.get("upload");
  useEffect(() => {
    if (uploadParam !== "1") return;
    openUploadZone("auto");
    // Strip it: the URL described an action, not a state, and leaving it there
    // would re-open the zone on every back/forward.
    setSearchParams((prev) => withParams(prev, { upload: null }), {
      replace: true,
    });
  }, [uploadParam, setSearchParams, openUploadZone]);

  // ?queue=1 — where the header's "Processing n / m" chip sends the user. The
  // panel then renders even on an empty queue, because arriving at a page that
  // says nothing is worse than arriving at one that says "nothing is
  // processing right now". Kept in the URL so the scroll-into-view below and a
  // browser back/forward both still land on it.
  const queuePanelForced = searchParams.get("queue") === "1";
  useEffect(() => {
    if (!queuePanelForced) return;
    const el = document.getElementById("processing-queue");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [queuePanelForced]);
  const [noteTransaction, setNoteTransaction] = useState<Transaction | null>(null);
  const [matchDialogOpen, setMatchDialogOpen] = useState(false);
  const [matchDialogTransaction, setMatchDialogTransaction] = useState<Transaction | null>(null);
  const [matchDialogMatchId, setMatchDialogMatchId] = useState<number | null>(null);
  // Proof-column filename click → open the document itself, rather than the
  // change-match dialog the rest of the cell opens.
  const [proofViewerDocId, setProofViewerDocId] = useState<number | null>(null);
  const [multiMatchOpen, setMultiMatchOpen] = useState(false);
  const [multiMatchTransaction, setMultiMatchTransaction] = useState<Transaction | null>(null);
  const [linkDialogOpen, setLinkDialogOpen] = useState(false);
  const [linkDialogTransaction, setLinkDialogTransaction] = useState<Transaction | null>(null);
  const [chainBreadcrumbs, setChainBreadcrumbs] = useState<ChainBreadcrumbEntry[]>([]);
  const [highlightId, setHighlightId] = useState<string | null>(null);
  // Guards the one-shot search fallback below so a highlight target that can
  // never be resolved (deleted txn) doesn't loop.
  const highlightSearchFallbackRef = useRef<string | null>(null);

  // ConfirmDialog states
  const [clearStatementsDialog, setClearStatementsDialog] = useState<{ open: boolean; step: 1 | 2 }>({ open: false, step: 1 });
  const [clearReceiptsDialog, setClearReceiptsDialog] = useState<{ open: boolean; step: 1 | 2 }>({ open: false, step: 1 });
  const [deleteStatementTarget, setDeleteStatementTarget] = useState<string | null>(null);
  const [deleteDocumentTarget, setDeleteDocumentTarget] = useState<number | null>(null);

  // Source files (bank statement tabs) query
  const { data: sourceFilesData } = useQuery<{ source_files: SourceFileInfo[] }>({
    queryKey: ["source-files"],
    queryFn: () => api.get("/api/transactions/source-files"),
  });

  // Uploaded receipts/documents query (persistent across page refresh)
  const { data: documentsData } = useQuery<{ documents: UploadedDocument[]; total: number }>({
    queryKey: ["receipts", "all"],
    queryFn: () => api.get("/api/receipts?per_page=500"),
  });

  const uploadedDocuments = documentsData?.documents ?? [];
  const totalDocuments = documentsData?.total ?? uploadedDocuments.length;
  const sourceFilesRaw = sourceFilesData?.source_files ?? [];

  // Sort tabs chronologically by min_date (primary), then account (secondary)
  const sourceFiles = useMemo(() => {
    return [...sourceFilesRaw].sort((a, b) => {
      const dateA = a.min_date ?? "";
      const dateB = b.min_date ?? "";
      if (dateA !== dateB) return dateA.localeCompare(dateB);
      return a.account.localeCompare(b.account);
    });
  }, [sourceFilesRaw]);

  // Derive activeSourceFile from URL, validated against loaded sourceFiles.
  // Default to first source file when no valid param (no "All Statements" tab).
  const statementParam = searchParams.get("statement");
  // `?statements=<file>` is the queue panel's "View" link for a statement that
  // has no tab of its own (see below). It still selects the tab when one exists,
  // so a stale link never dead-ends.
  const statementsListParam = searchParams.get("statements");
  const hasTab = useCallback(
    (name: string | null) =>
      name != null && sourceFiles.some((sf) => sf.source_file === name),
    [sourceFiles],
  );
  const activeSourceFile = hasTab(statementParam)
    ? statementParam
    : hasTab(statementsListParam)
      ? statementsListParam
      : sourceFiles.length > 0
        ? sourceFiles[0].source_file
        : null;

  // A statement can exist without a tab: tabs are keyed on
  // `transactions.source_file`, so a statement whose every row was already in
  // the books contributed no row that carries its name and never gets one.
  // Silently falling back to the first tab would show a different statement's
  // rows as if they were this file's — so name the file instead and say what
  // happened to it.
  const requestedStatement = statementParam ?? statementsListParam;
  const tablessStatement =
    sourceFiles.length > 0 && requestedStatement && !hasTab(requestedStatement)
      ? requestedStatement
      : null;

  // The currently-selected statement tab object (the purple bubble) — drives
  // the per-statement "Reconcile Statement" action's scope + label.
  const activeStatement =
    sourceFiles.find((sf) => sf.source_file === activeSourceFile) ?? null;

  // Tab change handler — pushes a history entry so back returns to the
  // previously selected statement.
  const handleStatementTab = useCallback(
    (value: string) => {
      setSearchParams((prev) =>
        // `statements` goes with it: picking a tab answers the "this file has no
        // tab" notice, so the notice must not survive the click.
        withParams(prev, { statement: value, statements: null, page: null }),
      );
    },
    [setSearchParams]
  );

  // Track source files before upload to auto-select new tabs
  const prevSourceFilesRef = useRef<string[]>([]);
  const didSnapshotRef = useRef(false);

  // When statement upload zone opens, snapshot current source files ONCE
  useEffect(() => {
    if (statementUploadOpen && !didSnapshotRef.current) {
      prevSourceFilesRef.current = sourceFiles.map((sf) => sf.source_file);
      didSnapshotRef.current = true;
    }
    if (!statementUploadOpen) {
      didSnapshotRef.current = false;
    }
  }, [statementUploadOpen, sourceFiles]);

  // When source files change after an upload, auto-select appropriately
  useEffect(() => {
    if (
      sourceFiles.length > 0 &&
      didSnapshotRef.current &&
      sourceFiles.length > prevSourceFilesRef.current.length
    ) {
      const newFiles = sourceFiles.filter(
        (sf) => !prevSourceFilesRef.current.includes(sf.source_file)
      );
      if (newFiles.length > 0) {
        // Auto-select the first new file (programmatic — replace, no entry)
        setSearchParams(
          (prev) =>
            withParams(prev, {
              statement: newFiles[0].source_file,
              page: null,
            }),
          { replace: true },
        );
        // Update the snapshot so this does not re-trigger
        prevSourceFilesRef.current = sourceFiles.map((sf) => sf.source_file);
      }
    }
  }, [sourceFiles, searchParams, setSearchParams]);

  // Analytics drill-through: ?category=X&start=YYYY-MM-DD&end=YYYY-MM-DD
  // When present, this filter crosses statements, so the per-tab source_file
  // scoping is suppressed and the inner filter state is bypassed.
  const categoryParam = searchParams.get("category");
  const startParam = searchParams.get("start");
  const endParam = searchParams.get("end");
  const hasAnalyticsFilter = Boolean(categoryParam && startParam && endParam);

  // Text search crosses statements: the search box promises TXN-ID lookup,
  // and a match in another statement's tab must not come back empty just
  // because a different tab happens to be selected.
  const hasSearchFilter = Boolean(filters.search);

  // Build query params
  const queryParams = new URLSearchParams();
  queryParams.set("page", String(page));
  queryParams.set("per_page", String(perPage));
  if (filters.account) queryParams.set("account", filters.account);
  if (filters.status) queryParams.set("status", filters.status);
  if (filters.month) queryParams.set("month", filters.month);
  if (filters.search) queryParams.set("search", filters.search);
  if (filters.min_amount) queryParams.set("min_amount", filters.min_amount);
  if (filters.max_amount) queryParams.set("max_amount", filters.max_amount);
  if (!hasAnalyticsFilter && !hasSearchFilter && activeSourceFile) {
    queryParams.set("source_file", activeSourceFile);
  }
  if (sort.column) {
    queryParams.set("sort_by", sort.column);
    queryParams.set("sort_dir", sort.direction.toUpperCase());
  }
  if (hasAnalyticsFilter) {
    queryParams.set("category", categoryParam!);
    queryParams.set("start", startParam!);
    queryParams.set("end", endParam!);
  }

  // Queries
  const {
    data: txnData,
    isLoading,
    isFetching,
    isPlaceholderData,
    isError: txnError,
    error: txnErrorObj,
    refetch: refetchTxn,
  } = useQuery<TransactionsResponse>({
    queryKey: [
      "transactions",
      filters,
      page,
      activeSourceFile,
      categoryParam,
      startParam,
      endParam,
      sort,
    ],
    queryFn: () => api.get(`/api/transactions?${queryParams.toString()}`),
    placeholderData: keepPreviousData,
  });

  const clearAnalyticsFilter = () => {
    setSearchParams((prev) =>
      withParams(prev, { category: null, start: null, end: null, page: null }),
    );
  };

  // Amount filtering now happens server-side (min_amount/max_amount query
  // params) so results span all pages and the pagination total stays honest.
  const filteredTransactions = txnData?.transactions ?? [];

  // Keep selectedTransaction in sync with latest query data so the
  // slide-over reflects proof mutations (add/remove) without manual refresh.
  useEffect(() => {
    if (!selectedTransaction || !txnData?.transactions) return;
    const fresh = txnData.transactions.find(
      (t) => t.id === selectedTransaction.id
    );
    if (fresh && fresh !== selectedTransaction) {
      setSelectedTransaction(fresh);
    }
  }, [txnData, selectedTransaction]);

  // Handle ?highlight= URL param for cross-statement navigation.
  // The highlight persists until the user takes ANY input action (mouse,
  // keyboard, touch, wheel) so it remains visible no matter how long the
  // tab switch / data load takes.
  const highlightParam = searchParams.get("highlight");
  useEffect(() => {
    if (!highlightParam || !txnData || isLoading) return;
    // Only judge the rows once they belong to the CURRENT params: a jump that
    // switches statement re-renders with the previous statement's rows still in
    // hand (keepPreviousData), and acting on those would mean chasing a row
    // that is merely not-loaded-yet.
    if (isFetching || isPlaceholderData) return;

    // The target row isn't necessarily on screen: a statement paginates at 50
    // rows, and any surviving filter (status/account/month/amount) can exclude
    // it — in either case the scroll below would find nothing and the jump
    // would silently do nothing. A bare txn id in the search box resolves
    // across every statement, so fall back to it once; this effect re-runs on
    // the refetched data and the highlight lands.
    const onCurrentPage = txnData.transactions.some(
      (t) => String(t.id) === highlightParam,
    );
    if (!onCurrentPage) {
      if (
        filters.search !== highlightParam &&
        highlightSearchFallbackRef.current !== highlightParam
      ) {
        highlightSearchFallbackRef.current = highlightParam;
        setSearchParams(
          (prev) => withParams(prev, { q: highlightParam, page: null }),
          { replace: true },
        );
      }
      return;
    }

    setHighlightId(highlightParam);

    const scrollTimer = setTimeout(() => {
      const row = document.querySelector(`tr[data-txn-id="${highlightParam}"]`);
      if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }, 200);

    const inputEvents = [
      "pointerdown",
      "mousedown",
      "keydown",
      "touchstart",
      "wheel",
    ] as const;
    let listenersAttached = false;

    const clearHighlight = () => {
      if (listenersAttached) {
        inputEvents.forEach((e) => window.removeEventListener(e, clearHighlight));
        listenersAttached = false;
      }
      setHighlightId(null);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("highlight");
          return next;
        },
        { replace: true },
      );
    };

    // Delay attaching input listeners so the click that triggered navigation
    // (and the smooth-scroll settle) doesn't immediately clear the highlight.
    const attachTimer = setTimeout(() => {
      inputEvents.forEach((e) =>
        window.addEventListener(e, clearHighlight, { passive: true }),
      );
      listenersAttached = true;
    }, 700);

    return () => {
      clearTimeout(scrollTimer);
      clearTimeout(attachTimer);
      if (listenersAttached) {
        inputEvents.forEach((e) => window.removeEventListener(e, clearHighlight));
        listenersAttached = false;
      }
    };
  }, [
    highlightParam,
    txnData,
    isLoading,
    isFetching,
    isPlaceholderData,
    filters.search,
    setSearchParams,
  ]);

  // Pending review count — uses a distinct query key from the /review page
  // to avoid per_page cache collision (this fetches per_page=1 for counts only)
  const { data: matchQueueData } = useQuery<{
    matches: unknown[];
    total: number;
    queue_counts: Record<string, number>;
  }>({
    queryKey: ["review-match-counts"],
    queryFn: () => api.get("/api/reconciliation/matches?status=PENDING_REVIEW,NEEDS_REVIEW&page=1&per_page=1"),
  });

  // Aggregate stats for the page header
  const { data: summaryData } = useQuery<{
    total_transactions: number;
    resolved_count: number;
    unresolved_count: number;
    matched_count: number;
    linked_count: number;
    ignored_count: number;
    reconcilable_count: number;
    resolution_rate: number;
  }>({
    queryKey: ["dashboard", "summary"],
    queryFn: () => api.get("/api/dashboard/summary"),
  });

  const { data: dateRangeData } = useQuery<DateRange>({
    queryKey: ["transactions-date-range"],
    queryFn: () => api.get("/api/transactions/date-range"),
    staleTime: 5 * 60 * 1000,
  });

  // Reconciliation progress state
  const [reconProgress, setReconProgress] = useState<ReconcileProgress | null>(null);
  const [reconResult, setReconResult] = useState<ReconcileResult | null>(null);
  const [, setReconError] = useState<string | null>(null);
  const [reconLogs, setReconLogs] = useState<string[]>([]);
  const [reconSaving, setReconSaving] = useState(false);
  const [reconLogsExpanded, setReconLogsExpanded] = useState(false);
  const [navigatingToReview, setNavigatingToReview] = useState(false);
  const [selectedTxnIds, setSelectedTxnIds] = useState<Set<number>>(new Set());
  const [focusedIndex, setFocusedIndex] = useState(-1);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const rateLimitedUntil = useAuthStore((s) => s.rateLimitedUntil);
  const [rateLimitCountdown, setRateLimitCountdown] = useState(0);

  // Reconciliation settings
  const { data: reconSettings } = useQuery<ReconSettings>({
    queryKey: ["recon-settings"],
    queryFn: () => api.get("/api/reconciliation/settings"),
    staleTime: 60_000,
  });
  const updateReconSettings = useMutation({
    mutationFn: (maxDays: number) =>
      api.put("/api/reconciliation/settings", { max_date_variance_days: maxDays }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["recon-settings"] }),
  });

  // Connection status (Wise / PayPal / Amazon)
  const { data: onboardingConfig } = useQuery<{
    sources?: {
      wise?: { connected: boolean; enabled: boolean };
      paypal?: { connected: boolean; enabled: boolean };
      amazon?: { connected: boolean; enabled: boolean };
    };
  }>({
    queryKey: ["onboarding-config"],
    queryFn: () => api.get("/api/onboarding/config"),
    staleTime: 60_000,
  });
  const wiseConnected = onboardingConfig?.sources?.wise?.connected ?? false;
  // Enabled but not connected means the stored token can no longer be read (e.g. the
  // server's credential-encryption secret changed). Show a reconnect path instead of
  // silently dropping the Sync Wise button off the header.
  const wiseEnabled = onboardingConfig?.sources?.wise?.enabled ?? false;
  const amazonConnected = onboardingConfig?.sources?.amazon?.connected ?? false;

  // Wise profile selector dialog
  const [wiseProfileDialogOpen, setWiseProfileDialogOpen] = useState(false);
  const [wiseProfiles, setWiseProfiles] = useState<WiseProfile[]>([]);
  const [wiseSelectedProfileId, setWiseSelectedProfileId] = useState<number | null>(null);
  const [wiseProfilesLoading, setWiseProfilesLoading] = useState(false);

  // Wise reconnect dialog (shown when the source is enabled but the token is unreadable)
  const [wiseReconnectOpen, setWiseReconnectOpen] = useState(false);
  const [wiseReconnectToken, setWiseReconnectToken] = useState("");
  const [wiseReconnectError, setWiseReconnectError] = useState("");
  const [wiseWalkthroughOpen, setWiseWalkthroughOpen] = useState(false);

  // Wise sync date range (shown in the pre-sync dialog)
  const [wiseSyncSince, setWiseSyncSince] = useState<string>(() => {
    const d = new Date();
    d.setDate(d.getDate() - 30);
    return d.toISOString().slice(0, 10); // "YYYY-MM-DD"
  });
  const [wiseSyncUntil, setWiseSyncUntil] = useState<string>(
    new Date().toISOString().slice(0, 10) // today
  );

  // Amazon import state. PayPal has no sync path: its transactions come in as
  // a CSV export the user uploads, the same as any other statement.
  const [amazonImportOpen, setAmazonImportOpen] = useState(false);

  const fetchWiseProfiles = useCallback(async () => {
    // Reset date range to default (last 30 days) each time dialog opens
    const today = new Date();
    const thirtyDaysAgo = new Date();
    thirtyDaysAgo.setDate(today.getDate() - 30);
    setWiseSyncUntil(today.toISOString().slice(0, 10));
    setWiseSyncSince(thirtyDaysAgo.toISOString().slice(0, 10));

    setWiseProfilesLoading(true);
    try {
      const data = await api.get<{ profiles: WiseProfile[]; selected_profile_id: number | null }>(
        "/api/actions/wise/profiles"
      );
      if (!data || !Array.isArray(data.profiles)) {
        console.error("Wise profiles response:", JSON.stringify(data));
        throw new Error(
          (data as Record<string, unknown>)?.detail as string
            || "Unexpected response from server — check that the backend is running the latest code"
        );
      }
      setWiseProfiles(data.profiles);
      setWiseSelectedProfileId(data.selected_profile_id);
      setWiseProfileDialogOpen(true);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to fetch Wise profiles";
      console.error("Failed to fetch Wise profiles:", err);
      toast.error(message);
    } finally {
      setWiseProfilesLoading(false);
    }
  }, []);

  // Shared success path for both reconnect routes (inline token, or the walkthrough):
  // refresh the connection state, close everything, and drop the user straight into sync.
  const handleWiseReconnected = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
    setWiseReconnectOpen(false);
    setWiseReconnectToken("");
    setWiseReconnectError("");
    toast.success("Wise reconnected");
    fetchWiseProfiles();
  }, [queryClient, fetchWiseProfiles]);

  const reconnectWiseMutation = useMutation({
    mutationFn: () =>
      api.post("/api/onboarding/wise/test", { token: wiseReconnectToken.trim() }),
    onSuccess: () => {
      handleWiseReconnected();
    },
    onError: (err: unknown) => {
      setWiseReconnectError(
        err instanceof ApiError ? err.message : "Couldn't reach Wise — try again."
      );
    },
  });

  const selectWiseProfile = useMutation({
    mutationFn: (profileId: number) =>
      api.post("/api/actions/wise/select-profile", { profile_id: profileId }),
    onSuccess: () => {
      setWiseProfileDialogOpen(false);
      // Auto-trigger sync after profile selection, passing the user-chosen date range
      syncWiseMutation.mutate({ since_date: wiseSyncSince, until_date: wiseSyncUntil });
    },
  });

  // Wise sync
  const [wiseSyncResult, setWiseSyncResult] = useState<{
    new: number;
    total: number;
    months: number;
  } | null>(null);
  const syncWiseMutation = useMutation({
    mutationFn: (dateRange?: { since_date: string; until_date: string }) =>
      api.post<{
        status: string;
        results: Record<string, { new?: number; total?: number; error?: string; skipped?: string }>;
      }>("/api/actions/sync", {
        sources: ["wise"],
        days: 30,
        auto_reconcile: false,
        since_date: dateRange?.since_date ?? undefined,
        until_date: dateRange?.until_date ?? undefined,
      }),
    onSuccess: (data) => {
      const results = data.results;

      // Check if profile selection is needed
      const wiseResult = results["wise"];
      if (wiseResult && "needs_profile_selection" in wiseResult && wiseResult.needs_profile_selection) {
        // Open profile selector dialog
        fetchWiseProfiles();
        return;
      }

      // Aggregate across ALL wise_* keys (wise_USD_202601, wise_CAD_202602, etc.)
      let totalNew = 0;
      let totalAll = 0;
      let monthCount = 0;
      for (const [key, val] of Object.entries(results)) {
        if (key.startsWith("wise_") && val && typeof val === "object" && "new" in val) {
          totalNew += val.new ?? 0;
          totalAll += val.total ?? 0;
          monthCount++;
        }
      }
      setWiseSyncResult({ new: totalNew, total: totalAll, months: monthCount });
      queryClient.invalidateQueries({ queryKey: ["source-files"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["transactions-summary"] });
      // Clear result after 8 seconds
      setTimeout(() => setWiseSyncResult(null), 8000);
    },
    onError: () => {
      toast.error("Wise sync failed. Check your connection and try again.");
    },
  });

  // Auto-scroll logs
  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [reconLogs]);

  // Stop polling helper
  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // After reconciliation completes, refetch all related queries and wait
  // for caches to be populated with the new DB state before enabling UI.
  const refetchAfterReconciliation = useCallback(async () => {
    setReconSaving(true);
    try {
      await Promise.all([
        queryClient.refetchQueries({ queryKey: ["transactions"] }),
        queryClient.refetchQueries({ queryKey: ["review-matches"] }),
        queryClient.refetchQueries({ queryKey: ["review-match-counts"] }),
        queryClient.refetchQueries({ queryKey: ["dashboard"] }),
        queryClient.refetchQueries({ queryKey: ["source-files"] }),
        queryClient.refetchQueries({ queryKey: ["receipts"] }),
      ]);
    } catch {
      // Fallback: queries will refresh on next access
    }
    setReconSaving(false);
  }, [queryClient]);

  // Session-refresh backoff: pause polling and tick down a countdown.
  //
  // `rateLimitedUntil` is set whenever a token refresh fails — a 429 from the
  // server's rate limiter, but equally a restart or a dropped socket. The
  // wording below says that rather than naming an identity provider: this
  // software issues its own sessions and there is no third-party auth service
  // to be throttled by.
  // Must be declared after refetchAfterReconciliation (used in resume polling).
  const refetchRef = useRef(refetchAfterReconciliation);
  refetchRef.current = refetchAfterReconciliation;

  useEffect(() => {
    if (!rateLimitedUntil || rateLimitedUntil <= Date.now()) {
      setRateLimitCountdown(0);
      return;
    }

    // Pause polling while rate-limited
    const wasPolling = pollingRef.current !== null;
    if (wasPolling) stopPolling();

    setRateLimitCountdown(Math.ceil((rateLimitedUntil - Date.now()) / 1000));
    setReconLogs((prev) => {
      const msg = "Session refresh failed — pausing until it can be retried...";
      return prev[prev.length - 1] === msg ? prev : [...prev, msg];
    });

    const ticker = setInterval(() => {
      const remaining = Math.ceil((rateLimitedUntil - Date.now()) / 1000);
      if (remaining <= 0) {
        setRateLimitCountdown(0);
        clearInterval(ticker);
        // Resume polling if reconciliation was in progress
        if (wasPolling && reconProgress?.status === "running") {
          setReconLogs((prev) => [...prev, "Cooldown complete — resuming reconciliation..."]);
          let idleCount = 0;
          let lastPhase = "";
          pollingRef.current = setInterval(async () => {
            try {
              const progress = await api.get<ReconcileProgress>("/api/reconciliation/progress");
              if (progress.status === "complete") {
                stopPolling();
                if (progress.logs && progress.logs.length > 0) {
                  setReconLogs((prev) => {
                    const newLogs = progress.logs!.filter((l: string) => !prev.includes(l));
                    return newLogs.length > 0 ? [...prev, ...newLogs] : prev;
                  });
                }
                const r = progress.result;
                const summaryParts = [`Reconciliation complete! ${summarizeMatchRun(r)}.`];
                if (r) {
                  const totalTxn = r.matched + r.remaining_transactions;
                  const totalDoc = r.matched + r.remaining_documents;
                  const matchedTxn = totalTxn - (r.remaining_transactions ?? 0);
                  const matchedDoc = totalDoc - (r.remaining_documents ?? 0);
                  summaryParts.push(`  Match rate: ${matchedTxn}/${totalTxn} (${Math.round(r.rate)}%)`);
                  summaryParts.push(`  Transactions: ${matchedTxn}/${totalTxn} matched`);
                  summaryParts.push(`  Receipts: ${matchedDoc}/${totalDoc} matched`);
                  if (r.refreshed) summaryParts.push(`  Already on file, re-scored (not new): ${r.refreshed}`);
                  if (r.flagged_for_review) summaryParts.push(`  Flagged for review, not matched: ${r.flagged_for_review}`);
                  if (r.displaced) summaryParts.push(`  Weak matches displaced by a better one: ${r.displaced}`);
                  if (r.llm_calls) summaryParts.push(`  AI agent calls: ${r.llm_calls} (${r.llm_fallbacks ?? 0} fell back to deterministic scoring)`);
                }
                setReconLogs((prev) => [...prev, ...summaryParts, "Saving results..."]);
                setReconProgress({ ...progress, percent: 100 });
                if (r) setReconResult(r);
                await refetchRef.current();
                setReconLogs((prev) => [...prev, "All results saved and synced."]);
                announceReconciliationComplete(r);
              } else if (progress.status === "error") {
                stopPolling();
                setReconError(progress.detail || "Unknown error");
                setReconProgress({ ...progress });
              } else if (progress.status === "running") {
                idleCount = 0;
                setReconProgress(progress);
                const phaseStr = `${progress.phase ?? ""} ${progress.detail ?? ""}`.trim();
                if (phaseStr && phaseStr !== lastPhase) {
                  lastPhase = phaseStr;
                  const pctStr = progress.percent != null ? ` (${progress.percent}%)` : "";
                  setReconLogs((prev) => [...prev, `${phaseStr}${pctStr}`]);
                }
              } else if (progress.status === "idle") {
                idleCount++;
                if (idleCount >= 3) {
                  stopPolling();
                  setReconLogs((prev) => [...prev, "Reconciliation finished.", "Saving results..."]);
                  setReconProgress({ status: "complete", percent: 100 });
                  await refetchRef.current();
                  setReconLogs((prev) => [...prev, "All results saved and synced."]);
                  announceReconciliationComplete(null);
                }
              }
            } catch {
              // Transient fetch error — keep polling
            }
          }, 1500);
        }
      } else {
        setRateLimitCountdown(remaining);
      }
    }, 1000);

    return () => clearInterval(ticker);
  }, [rateLimitedUntil, stopPolling, reconProgress?.status]);


  // Start reconciliation and begin polling.
  //
  // mode === "statement" reconciles ONLY the transactions of the given
  // statement tab (sourceFile), matched against the full unmatched-receipt
  // pool. "remaining" and "all" keep their whole-account semantics.
  const startReconciliation = useCallback(async (
    mode: "remaining" | "all" | "statement" = "remaining",
    sourceFile?: string,
  ) => {
    const isAll = mode === "all";
    const isStatement = mode === "statement";
    const label = isAll
      ? "Re-matching everything (starting over)"
      : isStatement
        ? "Matching this statement"
        : "Matching receipts";
    const endpoint = isAll
      ? "/api/reconciliation/reset-and-start"
      : isStatement
        ? "/api/reconciliation/start-statement"
        : "/api/actions/reconcile";

    setReconResult(null);
    setReconError(null);
    setReconLogs([`${label}...`]);
    setReconProgress({ status: "running", phase: "Starting...", percent: 0 });

    try {
      await api.post<ReconcileStartResponse>(
        endpoint,
        isStatement ? { source_file: sourceFile } : undefined,
      );
      setReconLogs((prev) => [...prev, "Reconciliation request sent, waiting for progress..."]);
    } catch (err) {
      setReconProgress({ status: "error", percent: 0 });
      setReconError(err instanceof Error ? err.message : "Failed to start reconciliation");
      setReconLogs((prev) => [...prev, `ERROR: Failed to start - ${err}`]);
      return;
    }

    // Start polling every 1.5 seconds for smoother progress
    stopPolling();
    let idleCount = 0;
    let lastPhase = "";
    pollingRef.current = setInterval(async () => {
      try {
        const progress = await api.get<ReconcileProgress>("/api/reconciliation/progress");

        if (progress.status === "complete") {
          stopPolling();
          // Append server-side logs before final message
          if (progress.logs && progress.logs.length > 0) {
            setReconLogs((prev) => {
              const newLogs = progress.logs!.filter((l) => !prev.includes(l));
              return newLogs.length > 0 ? [...prev, ...newLogs] : prev;
            });
          }
          const r = progress.result;
          const summaryParts = [`Reconciliation complete! ${summarizeMatchRun(r)}.`];
          if (r) {
            const totalTxn = r.matched + r.remaining_transactions;
            const totalDoc = r.matched + r.remaining_documents;
            const matchedTxn = totalTxn - (r.remaining_transactions ?? 0);
            const matchedDoc = totalDoc - (r.remaining_documents ?? 0);
            summaryParts.push(`  Match rate: ${matchedTxn}/${totalTxn} (${Math.round(r.rate)}%)`);
            summaryParts.push(`  Transactions: ${matchedTxn}/${totalTxn} matched (${totalTxn > 0 ? Math.round(matchedTxn / totalTxn * 100) : 0}%)`);
            summaryParts.push(`  Receipts: ${matchedDoc}/${totalDoc} matched (${totalDoc > 0 ? Math.round(matchedDoc / totalDoc * 100) : 0}%)`);
            if (r.refreshed) summaryParts.push(`  Already on file, re-scored (not new): ${r.refreshed}`);
            if (r.flagged_for_review) summaryParts.push(`  Flagged for review, not matched: ${r.flagged_for_review}`);
            if (r.displaced) summaryParts.push(`  Weak matches displaced by a better one: ${r.displaced}`);
            if (r.llm_calls) summaryParts.push(`  AI agent calls: ${r.llm_calls} (${r.llm_fallbacks ?? 0} fell back to deterministic scoring)`);
          }
          setReconLogs((prev) => [...prev, ...summaryParts, "Saving results..."]);
          setReconProgress({ ...progress, percent: 100 });
          if (r) setReconResult(r);
          // Wait for all caches to be populated with DB data before enabling UI
          await refetchAfterReconciliation();
          setReconLogs((prev) => [...prev, "All results saved and synced."]);
          announceReconciliationComplete(r);
          // Auto-navigate to review if there are pending items
          try {
            const reviewData = queryClient.getQueryData<{ queue_counts?: Record<string, number> }>(["review-matches"]);
            const qc = reviewData?.queue_counts ?? {};
            const pending = (qc["PENDING_REVIEW"] ?? 0) + (qc["NEEDS_REVIEW"] ?? 0);
            if (pending > 0) {
              setTimeout(() => navigate("/review"), 1200);
            }
          } catch { /* proceed without navigation */ }
        } else if (progress.status === "error") {
          stopPolling();
          const errMsg = progress.detail || "Unknown error";
          setReconError(errMsg);
          // Append server-side error logs
          if (progress.logs && progress.logs.length > 0) {
            setReconLogs((prev) => {
              const newLogs = progress.logs!.filter((l) => !prev.includes(l));
              return newLogs.length > 0 ? [...prev, ...newLogs] : prev;
            });
          }
          setReconLogs((prev) => [...prev, `ERROR: ${errMsg}`]);
          setReconProgress({ ...progress });
          toast.error(`Matching failed — ${errMsg}`);
        } else if (progress.status === "running") {
          idleCount = 0;
          setReconProgress(progress);
          // Add log entry when phase changes
          const phaseStr = `${progress.phase ?? ""} ${progress.detail ?? ""}`.trim();
          if (phaseStr && phaseStr !== lastPhase) {
            lastPhase = phaseStr;
            const pctStr = progress.percent != null ? ` (${progress.percent}%)` : "";
            setReconLogs((prev) => [...prev, `${phaseStr}${pctStr}`]);
          }
          // Append server-side logs if present
          if (progress.logs && progress.logs.length > 0) {
            setReconLogs((prev) => {
              const newLogs = progress.logs!.filter((l) => !prev.includes(l));
              return newLogs.length > 0 ? [...prev, ...newLogs] : prev;
            });
          }
        } else if (progress.status === "idle") {
          idleCount++;
          if (idleCount >= 3) {
            stopPolling();
            setReconLogs((prev) => [...prev, "Reconciliation finished (no further progress updates).", "Saving results..."]);
            setReconProgress({ status: "complete", percent: 100 });
            // Wait for all caches to be populated with DB data before enabling UI
            await refetchAfterReconciliation();
            setReconLogs((prev) => [...prev, "All results saved and synced."]);
            announceReconciliationComplete(null);
            // Auto-navigate to review if there are pending items
            try {
              const reviewData = queryClient.getQueryData<{ queue_counts?: Record<string, number> }>(["review-matches"]);
              const qc2 = reviewData?.queue_counts ?? {};
              const pending2 = (qc2["PENDING_REVIEW"] ?? 0) + (qc2["NEEDS_REVIEW"] ?? 0);
              if (pending2 > 0) {
                setTimeout(() => navigate("/review"), 1200);
              }
            } catch { /* proceed without navigation */ }
          }
        }
      } catch {
        // Transient fetch error — keep polling
      }
    }, 1500);
  }, [queryClient, stopPolling, refetchAfterReconciliation]);

  const isReconciling = reconProgress !== null && reconProgress.status === "running";
  const isBusy = isReconciling || reconSaving;

  // Uploaded-files panel filters — URL-backed so back/forward restores them.
  // Absent param = default; the literal "all" encodes a user-cleared date.
  const pfParam = searchParams.get("pf");
  const proofFilter: "all" | "MATCHED" | "EXTRACTED" =
    pfParam === "MATCHED" || pfParam === "EXTRACTED" ? pfParam : "all";
  const setProofFilter = useCallback(
    (v: "all" | "MATCHED" | "EXTRACTED") =>
      setSearchParams((prev) =>
        withParams(prev, { pf: v === "all" ? null : v }),
      ),
    [setSearchParams],
  );
  const proofSearch = searchParams.get("ps") ?? "";
  const setProofSearch = useCallback(
    (v: string) =>
      setSearchParams((prev) => withParams(prev, { ps: v }), { replace: true }),
    [setSearchParams],
  );

  // Date filter state for uploaded files sections
  const currentYear = new Date().getFullYear();
  const defaultDateFrom = `${currentYear}-01-01`;
  const defaultDateTo = new Date().toISOString().slice(0, 10);
  const dateParam = useCallback(
    (key: string, fallback: string): string => {
      const v = searchParams.get(key);
      if (v === null) return fallback;
      return v === "all" ? "" : v;
    },
    [searchParams],
  );
  const setDateParam = useCallback(
    (key: string) => (v: string) =>
      setSearchParams(
        (prev) => withParams(prev, { [key]: v === "" ? "all" : v }),
        { replace: true },
      ),
    [setSearchParams],
  );
  const statementsDateFrom = dateParam("sfrom", defaultDateFrom);
  const statementsDateTo = dateParam("sto", defaultDateTo);
  const proofsDateFrom = dateParam("pfrom", defaultDateFrom);
  const proofsDateTo = dateParam("pto", defaultDateTo);
  const setStatementsDateFrom = setDateParam("sfrom");
  const setStatementsDateTo = setDateParam("sto");
  const setProofsDateFrom = setDateParam("pfrom");
  const setProofsDateTo = setDateParam("pto");

  // An empty bound means UNBOUNDED, not "match nothing". Clearing the date
  // field writes "all", which dateParam maps to "" — and `date <= ""` is false
  // for every real date, so widening the filter to find a missing receipt
  // would otherwise hide every dated row instead.
  const withinDateRange = useCallback(
    (value: string | null | undefined, from: string, to: string): boolean => {
      if (!value) return true;               // undated rows are always shown
      if (from && value < from) return false;
      if (to && value > to) return false;
      return true;
    },
    [],
  );

  // Filtered source files by date range
  const filteredSourceFiles = useMemo(() => {
    return sourceFiles.filter((sf) => {
      // If statement has no dates, always show it
      if (!sf.min_date && !sf.max_date) return true;
      // Range overlap: statement overlaps filter if min_date <= filterTo AND max_date >= filterFrom
      const sfMin = sf.min_date ?? sf.max_date!;
      const sfMax = sf.max_date ?? sf.min_date!;
      if (statementsDateTo && sfMin > statementsDateTo) return false;
      if (statementsDateFrom && sfMax < statementsDateFrom) return false;
      return true;
    });
  }, [sourceFiles, statementsDateFrom, statementsDateTo]);

  // Filtered proofs by date range, status, and search (vendor/filename/receipt ID)
  const filteredDocuments = useMemo(() => {
    const q = proofSearch.trim();
    // Receipt ids render with a leading "#" — accept it when pasted back in.
    const qId = parseSearchId(q);
    return uploadedDocuments
      .filter((doc) => proofFilter === "all" || doc.status === proofFilter)
      .filter((doc) => {
        // A search reaches the user's WHOLE history. Chained after the date
        // window it would not: a receipt dated outside the default range
        // could then be recovered by no query at all.
        if (q) return true;
        return withinDateRange(doc.date, proofsDateFrom, proofsDateTo);
      })
      .filter((doc) => {
        if (!q) return true;
        // Exhaust every criterion — id (exact), vendor/filename (substring,
        // then OCR-tolerant), then the exact dollar amount — so a receipt is
        // findable by the text a human reads off the document even when the
        // extractor misread it.
        if (qId !== null && doc.id === qId) return true;
        if (fuzzyTextMatch(doc.filename, q) || fuzzyTextMatch(doc.vendor, q)) return true;
        return amountMatchesSearch(doc.total, q);
      });
  }, [uploadedDocuments, proofFilter, proofsDateFrom, proofsDateTo, proofSearch, withinDateRange]);

  // Delete statement mutation
  const deleteStatementMutation = useMutation<{ deleted: number }, Error, string>({
    mutationFn: (sourceFile) => api.delete(`/api/transactions/source-files/${encodeURIComponent(sourceFile)}`),
    onSuccess: (data, sourceFile) => {
      const n = data?.deleted ?? 0;
      toast.success(
        `Deleted ${sourceFile} — ${n} transaction${n === 1 ? "" : "s"} removed.`,
      );
      queryClient.invalidateQueries({ queryKey: ["source-files"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["review-matches"] });
      queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
      queryClient.invalidateQueries({ queryKey: ["receipts"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  // Delete document mutation
  const invalidateAfterDocDelete = () => {
    queryClient.invalidateQueries({ queryKey: ["receipts"] });
    queryClient.invalidateQueries({ queryKey: ["transactions"] });
    queryClient.invalidateQueries({ queryKey: ["source-files"] });
    queryClient.invalidateQueries({ queryKey: ["review-matches"] });
    queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
  };
  const deleteDocumentMutation = useMutation<{ deleted: boolean }, Error, number>({
    mutationFn: (docId) => api.delete(`/api/receipts/${docId}`),
    onSuccess: () => {
      toast.success("Receipt deleted.");
      invalidateAfterDocDelete();
    },
    onError: (err) => {
      // 404 = row already gone (stale UI, cross-tab delete, duplicate purge).
      // The desired state is reached — clear caches so the ghost card disappears.
      if (err instanceof ApiError && err.status === 404) {
        toast.success("Receipt deleted.");
        invalidateAfterDocDelete();
      } else {
        toast.error("Couldn't delete that receipt — please try again.");
      }
    },
  });

  // Clear all statements mutation
  const clearAllStatementsMutation = useMutation<{ deleted: number }, Error, void>({
    mutationFn: () => api.post("/api/transactions/source-files/clear-all", {}),
    onSuccess: (data) => {
      const n = data?.deleted ?? 0;
      toast.success(
        `All statements cleared — ${n} transaction${n === 1 ? "" : "s"} removed.`,
      );
      queryClient.invalidateQueries({ queryKey: ["source-files"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["review-matches"] });
      queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
      queryClient.invalidateQueries({ queryKey: ["receipts"] });
    },
  });

  // Clear all receipts mutation
  const clearAllDocumentsMutation = useMutation<{ deleted: number }, Error, void>({
    mutationFn: () => api.post("/api/receipts/clear-all", {}),
    onSuccess: (data) => {
      const n = data?.deleted ?? 0;
      toast.success(`All receipts deleted — ${n} removed.`);
      queryClient.invalidateQueries({ queryKey: ["receipts"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["source-files"] });
      queryClient.invalidateQueries({ queryKey: ["review-matches"] });
      queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
    },
  });

  // Recategorize-all: wipe every category and re-run the final LLM
  // categorization step across all transactions. The backend thread fires
  // `transactions_updated` events after each batch so the table refills live.
  const [recategorizing, setRecategorizing] = useState(false);
  const recategorizeMutation = useMutation<
    { status: string },
    Error,
    void
  >({
    mutationFn: () => api.post("/api/transactions/recategorize-all", {}),
    onMutate: () => {
      setRecategorizing(true);
      // Optimistically clear categories locally so the UI visibly resets
      // immediately — the live backend updates will refill them batch by batch.
      queryClient.setQueriesData<TransactionsResponse>(
        { queryKey: ["transactions"] },
        (old) => {
          if (!old) return old;
          return {
            ...old,
            transactions: old.transactions.map((t) => ({ ...t, category: null })),
          };
        },
      );
    },
    onError: () => {
      setRecategorizing(false);
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      // Close the recategorize progress modal that was opened optimistically
      // on click, so a failed run does not leave it spinning.
      setRecategorizeModalOpen(false);
    },
  });

  // Poll recategorize progress while a run is in flight, so the live agent log
  // window and the "X/Y" summary appear without waiting on the SSE event.
  const { data: recategorizeProgress } = useQuery<{
    status: "idle" | "running" | "complete" | "error";
    total?: number;
    done?: number;
    phase?: string;
    percent?: number;
    logs?: string[];
    summary?: {
      total?: number;
      rule_locked?: number;
      vendors_categorized?: number;
      txns_refined?: number;
      categories_final?: number;
      llm_calls?: number;
    } | null;
    error?: string | null;
  }>({
    queryKey: ["recategorize-progress"],
    queryFn: () => api.get("/api/transactions/recategorize-all/progress"),
    enabled: recategorizing,
    refetchInterval: recategorizing ? 1500 : false,
  });

  // Modal visibility: open when the user clicks Recategorize, stays open after
  // completion so the user can review the final summary before closing.
  const [recategorizeModalOpen, setRecategorizeModalOpen] = useState(false);

  // Flip the recategorizing flag off when the server reports a terminal state.
  // Use refetchQueries (not invalidateQueries) so the filtered transactions
  // view is guaranteed to pull fresh rows immediately instead of keeping the
  // optimistically-wiped cache entries from onMutate.
  useEffect(() => {
    if (!recategorizing) return;
    const status = recategorizeProgress?.status;
    if (status === "complete" || status === "error") {
      setRecategorizing(false);
      queryClient.refetchQueries({ queryKey: ["transactions"], type: "all" });
      queryClient.refetchQueries({ queryKey: ["dashboard"], type: "all" });
      queryClient.refetchQueries({ queryKey: ["expense-categories"], type: "all" });
    }
  }, [recategorizing, recategorizeProgress?.status, queryClient]);

  // Canonical fixed ISED/GIFI taxonomy for the manual-edit pickers. `flat` keeps
  // the old `expenseCategories` name so downstream uses compile unchanged;
  // `groups` drives the grouped optgroup pickers.
  const { flat: expenseCategories, groups: categoryGroups } = useCategoryTaxonomy();

  // Single-transaction category mutation used by both the table cell and the
  // detail slide-over. Optimistically updates the transactions cache so the
  // change is visible immediately.
  const categoryMutation = useMutation<
    { status: string; transaction_id: number; category: string | null },
    Error,
    { transactionId: string; category: string }
  >({
    mutationFn: ({ transactionId, category }) =>
      api.patch(`/api/transactions/${transactionId}/category`, { category }),
    onMutate: async ({ transactionId, category }) => {
      await queryClient.cancelQueries({ queryKey: ["transactions"] });
      await queryClient.cancelQueries({ queryKey: ["transaction", transactionId] });
      const prevList = queryClient.getQueriesData<TransactionsResponse>({
        queryKey: ["transactions"],
      });
      const prevDetail = queryClient.getQueryData(["transaction", transactionId]);
      queryClient.setQueriesData<TransactionsResponse>(
        { queryKey: ["transactions"] },
        (old) => {
          if (!old) return old;
          return {
            ...old,
            transactions: old.transactions.map((t) =>
              t.id === transactionId ? { ...t, category } : t
            ),
          };
        }
      );
      queryClient.setQueryData<Record<string, unknown> | undefined>(
        ["transaction", transactionId],
        (old) => (old ? { ...old, category } : old)
      );
      return { prevList, prevDetail, transactionId };
    },
    onSuccess: (_data, { category }) => {
      // The row updates optimistically, so without this the only signal that
      // the change reached the server is nothing happening.
      toast.success(
        category ? `Category set to ${category}.` : "Category cleared.",
      );
    },
    onError: (_err, _vars, context) => {
      const ctx = context as
        | { prevList?: [readonly unknown[], unknown][]; prevDetail?: unknown; transactionId?: string }
        | undefined;
      if (ctx?.prevList) {
        ctx.prevList.forEach(([key, data]) => queryClient.setQueryData(key, data));
      }
      if (ctx?.transactionId !== undefined) {
        queryClient.setQueryData(["transaction", ctx.transactionId], ctx.prevDetail);
      }
      toast.error("Couldn't update category — change reverted.");
    },
    onSettled: (_data, _err, vars) => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["transaction", vars.transactionId] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const handleCategoryChange = useCallback(
    (txn: Transaction, nextCategory: string) => {
      categoryMutation.mutate({ transactionId: txn.id, category: nextCategory });
    },
    [categoryMutation]
  );

  // Toggle ignore — backend flips status IGNORED <-> prior (re-derived from
  // active matches/links). The row is updated optimistically so the status
  // badge flips immediately.
  const toggleIgnoreMutation = useMutation<
    { status: string; transaction_id: number; new_status: Transaction["status"] },
    Error,
    { transactionId: string }
  >({
    mutationFn: ({ transactionId }) =>
      api.patch(`/api/transactions/${transactionId}/toggle-ignore`, {}),
    onSuccess: (data) => {
      toast.success(
        data?.new_status === "IGNORED"
          ? "Marked as no receipt needed."
          : "Receipt required again.",
      );
    },
    onError: () => {
      toast.error("Couldn't update that transaction — please try again.");
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const handleToggleIgnore = useCallback(
    (txn: Transaction) => {
      toggleIgnoreMutation.mutate({ transactionId: txn.id });
    },
    [toggleIgnoreMutation]
  );

  // View file helper.
  //
  // A blank tab is opened synchronously inside the click handler so the
  // browser treats it as a user-initiated popup. The async fetch then
  // navigates that already-open tab once the blob is ready. Without the
  // pre-open, Chrome and Safari block the later window.open() because the
  // user-gesture activation has already expired by the time the fetch
  // resolves.
  const viewFile = useCallback(async (url: string) => {
    const newWin = window.open("about:blank", "_blank", "noopener,noreferrer");
    try {
      const { getAccessToken } = useAuthStore.getState();
      const token = await getAccessToken();
      const res = await fetch(`${API_URL}${url}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        redirect: "follow",
      });
      if (!res.ok) {
        if (newWin) newWin.close();
        return;
      }
      const blob = await res.blob();
      const blobUrl = URL.createObjectURL(blob);
      if (newWin) {
        newWin.location.href = blobUrl;
      } else {
        window.open(blobUrl, "_blank", "noopener,noreferrer");
      }
    } catch {
      if (newWin) newWin.close();
    }
  }, []);

  const handleRowClick = useCallback((txn: Transaction) => {
    setSelectedTransaction(txn);
    setSlideOverOpen(true);
  }, []);

  // -- Keyboard shortcuts (J/K navigate, Enter open, Escape close, I ignore) --

  // Reset focused index when page or data changes
  useEffect(() => {
    setFocusedIndex(-1);
  }, [page, filters, activeSourceFile]);

  // Compute focused transaction ID for highlighting
  const focusedTxnId =
    focusedIndex >= 0 && filteredTransactions?.[focusedIndex]
      ? filteredTransactions[focusedIndex].id
      : null;

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      // Don't intercept when any dialog/modal is open
      if (slideOverOpen || matchDialogOpen || linkDialogOpen || multiMatchOpen) return;
      if (proofViewerDocId !== null) return;
      if (wiseProfileDialogOpen || wiseReconnectOpen || amazonImportOpen) return;
      if (uploadOpen || statementUploadOpen) return;

      const transactions = filteredTransactions;
      if (!transactions || transactions.length === 0) return;

      switch (e.key) {
        case "j":
        case "ArrowDown": {
          e.preventDefault();
          const next = Math.min(focusedIndex + 1, transactions.length - 1);
          setFocusedIndex(next);
          const row = document.querySelector(`[data-txn-id="${transactions[next].id}"]`);
          row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
          break;
        }
        case "k":
        case "ArrowUp": {
          e.preventDefault();
          const prev = Math.max(focusedIndex - 1, 0);
          setFocusedIndex(prev);
          const row = document.querySelector(`[data-txn-id="${transactions[prev].id}"]`);
          row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
          break;
        }
        case "Enter":
          if (focusedIndex >= 0 && focusedIndex < transactions.length) {
            e.preventDefault();
            handleRowClick(transactions[focusedIndex]);
          }
          break;
        case "Escape":
          e.preventDefault();
          if (focusedIndex >= 0) {
            setFocusedIndex(-1);
          }
          break;
        case "i":
          if (focusedIndex >= 0 && focusedIndex < transactions.length) {
            e.preventDefault();
            handleToggleIgnore(transactions[focusedIndex]);
          }
          break;
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    focusedIndex,
    filteredTransactions,
    slideOverOpen,
    matchDialogOpen,
    linkDialogOpen,
    multiMatchOpen,
    proofViewerDocId,
    wiseProfileDialogOpen,
    wiseReconnectOpen,
    amazonImportOpen,
    uploadOpen,
    statementUploadOpen,
    handleRowClick,
    handleToggleIgnore,
  ]);

  // Also handle Escape to close the slide-over (works even when other shortcuts are blocked)
  useEffect(() => {
    function handleEscapeSlideOver(e: KeyboardEvent) {
      if (e.key === "Escape" && slideOverOpen) {
        e.preventDefault();
        setSlideOverOpen(false);
        setSelectedTransaction(null);
      }
    }
    window.addEventListener("keydown", handleEscapeSlideOver);
    return () => window.removeEventListener("keydown", handleEscapeSlideOver);
  }, [slideOverOpen]);

  const handleFiltersChange = useCallback(
    (newFilters: TransactionFiltersState) => {
      // Pill selections push a history entry; debounced typing (search /
      // min / max) replaces so history isn't spammed per keystroke.
      const discreteChanged =
        newFilters.account !== filters.account ||
        newFilters.status !== filters.status ||
        newFilters.month !== filters.month;
      setSearchParams(
        (prev) =>
          withParams(prev, {
            account: newFilters.account || null,
            status: newFilters.status || null,
            fmonth: newFilters.month || null,
            q: newFilters.search || null,
            min: newFilters.min_amount || null,
            max: newFilters.max_amount || null,
            page: null,
          }),
        { replace: !discreteChanged },
      );
    },
    [filters, setSearchParams]
  );

  const handleProofClick = useCallback((txn: Transaction) => {
    setMatchDialogTransaction(txn);
    setMatchDialogMatchId(txn.match_id ?? null);
    setMatchDialogOpen(true);
  }, []);

  const handleMatchSuccess = useCallback(() => {
    setMatchDialogOpen(false);
    setMatchDialogTransaction(null);
    setMatchDialogMatchId(null);
  }, []);

  const addProof = useAddProof();

  const handleAttachAnother = useCallback((txn: Transaction) => {
    setMultiMatchTransaction(txn);
    setMultiMatchOpen(true);
  }, []);

  const handleMultiMatchSuccess = useCallback((docIds: number[]) => {
    if (!multiMatchTransaction) return;
    addProof.mutate(
      { txnId: parseInt(multiMatchTransaction.id, 10), documentIds: docIds },
      {
        onSuccess: () => {
          setMultiMatchOpen(false);
          setMultiMatchTransaction(null);
        },
      },
    );
  }, [multiMatchTransaction, addProof]);

  const handleLinkTransactionClick = useCallback((txn: Transaction) => {
    setLinkDialogTransaction(txn);
    setLinkDialogOpen(true);
  }, []);

  const handleChainNavigate = useCallback(
    (txnId: number, sourceFile: string) => {
      // Add current transaction to breadcrumbs
      if (selectedTransaction) {
        setChainBreadcrumbs((prev) => [
          ...prev,
          {
            transaction_id: parseInt(selectedTransaction.id, 10),
            account: selectedTransaction.account,
            amount: String(selectedTransaction.amount),
            currency: selectedTransaction.currency,
            source_file: selectedTransaction.source_file ?? "",
          },
        ]);
      }
      setSlideOverOpen(false);
      // Navigate to the linked transaction's tab
      setSearchParams((prev) =>
        withParams(prev, {
          statement: sourceFile,
          highlight: String(txnId),
          page: null,
        }),
      );
    },
    [selectedTransaction, setSearchParams]
  );

  // Jump from a receipt/invoice card to the transaction it proves, blinking the
  // row on arrival — same ?statement=&highlight= mechanism as the linked-
  // transaction navigation above. Table-scoping filters are cleared so the
  // target row can't be filtered out from under the highlight; the proof-panel
  // state (pf/ps) survives, so the user keeps their place in the receipt list.
  const jumpToLinkedTransaction = useCallback(
    (txnId: number, sourceFile: string | null) => {
      const changes: Record<string, string | null> = {
        highlight: String(txnId),
        page: null,
        status: null,
        q: null,
        account: null,
        fmonth: null,
        min: null,
        max: null,
        category: null,
        start: null,
        end: null,
      };
      // Only retarget the statement tab when the holding tab is known;
      // dropping the param would bounce the user to the first statement.
      if (sourceFile) changes.statement = sourceFile;
      setSearchParams((prev) => withParams(prev, changes));
    },
    [setSearchParams]
  );

  // Derive pending count from queue_counts
  const queueCounts = matchQueueData?.queue_counts ?? {};
  const pendingCount =
    (queueCounts["PENDING_REVIEW"] ?? 0) + (queueCounts["NEEDS_REVIEW"] ?? 0);

  // Navigate to review page — ensures fresh data is loaded before navigation
  const handleTakeAction = useCallback(async () => {
    setNavigatingToReview(true);
    try {
      await queryClient.refetchQueries({ queryKey: ["review-matches"] });
    } catch {
      // proceed with navigation regardless
    }
    navigate("/review");
  }, [queryClient, navigate]);

  if (txnError) {
    return (
      <ErrorDisplay error={txnErrorObj} context="transactions" onRetry={refetchTxn} />
    );
  }

  return (
    <div className="space-y-6 pt-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-3xl font-bold tracking-tight text-foreground">
            Transactions
          </h2>
          <p className="text-muted-foreground mt-1">
            Upload statements and receipts, then match them together.
          </p>
          {summaryData && summaryData.total_transactions > 0 && (
            <p className="text-xs text-muted-foreground mt-1.5">
              <span className="font-semibold text-foreground">{summaryData.resolved_count ?? 0}</span>
              {" of "}
              <span className="font-semibold text-foreground">{summaryData.reconcilable_count ?? summaryData.total_transactions}</span>
              {" transactions done"}
              {(summaryData.unresolved_count ?? 0) > 0 && (
                <>
                  {" \u00B7 "}
                  <span className={cn(
                    "font-semibold",
                    (summaryData.resolution_rate ?? 0) >= 80 ? "text-primary" : "text-amber-600"
                  )}>
                    {summaryData.unresolved_count} still need a receipt, a link, or "no receipt needed"
                  </span>
                </>
              )}
              {(summaryData.unresolved_count ?? 0) === 0 && (
                <span className="font-semibold text-success">{" \u00B7 all caught up"}</span>
              )}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            onClick={() => {
              setUploadMode("receipt");
              setUploadOpen(!uploadOpen);
              setStatementUploadOpen(false);
            }}
            className="border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
          >
            <Upload className="h-4 w-4 mr-2" />
            Upload Receipts & Invoices
          </Button>
          <Button
            onClick={() => startReconciliation("remaining")}
            disabled={isBusy}
            className="bg-primary hover:bg-primary/90 text-primary-foreground shadow-warm-sm"
          >
            {isBusy ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4 mr-2" />
            )}
            Match Receipts
          </Button>
          <Button
            variant="outline"
            onClick={() => { setStatementUploadOpen(!statementUploadOpen); setUploadOpen(false); }}
            className="border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
          >
            <FileText className="h-4 w-4 mr-2" />
            Upload Statements
          </Button>
          {(wiseConnected || wiseEnabled) && (
            wiseConnected ? (
              <Button
                variant="outline"
                onClick={() => fetchWiseProfiles()}
                disabled={syncWiseMutation.isPending}
                className="border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
              >
                <ArrowLeftRight className="h-4 w-4 mr-2" />
                {syncWiseMutation.isPending ? "Syncing Wise..." : "Sync Wise"}
              </Button>
            ) : (
              <Button
                variant="outline"
                onClick={() => setWiseReconnectOpen(true)}
                className="border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
              >
                <ArrowLeftRight className="h-4 w-4 mr-2" />
                Reconnect Wise
              </Button>
            )
          )}
          {amazonConnected && (
            <Button
              variant="outline"
              onClick={() => setAmazonImportOpen(true)}
              className="border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
            >
              <ShoppingCart className="h-4 w-4 mr-2" />
              Import Amazon
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => startReconciliation("all")}
            disabled={isBusy}
            className="border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
          >
            <RefreshCw className="h-4 w-4 mr-2" />
            Re-match All (start over)
          </Button>
          <div
            className="flex items-center gap-2"
            title="How many days apart a receipt date and a bank charge date can be and still count as a match"
          >
            <label className="text-xs text-muted-foreground whitespace-nowrap">Match window</label>
            <select
              value={reconSettings?.max_date_variance_days ?? 45}
              onChange={(e) => updateReconSettings.mutate(Number(e.target.value))}
              disabled={isBusy}
              className="h-9 rounded-md border border-border/60 bg-background px-2 text-xs focus:outline-none focus:ring-1 focus:ring-primary/50"
            >
              {(reconSettings?.options ?? [7, 14, 21, 30, 45, 60, 90]).map((d) => (
                <option key={d} value={d}>{d} days</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Workflow continuation CTA */}
      <TransactionsPagePrompt />

      {/* Keyboard shortcut hints */}
      <div
        className="hidden md:flex items-center gap-2 rounded-xl bg-muted/50 border border-border/40 px-3 py-2"
        title="Keyboard shortcuts: J/K=Navigate rows, Enter=Open, Esc=Close, I=Toggle 'no receipt needed' (the amount still counts)"
      >
        <Keyboard className="h-3.5 w-3.5 text-muted-foreground/60" />
        <span className="text-xs text-muted-foreground/70">
          <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
            J
          </kbd>{" / "}
          <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
            K
          </kbd>{" "}
          Navigate{" "}
          <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
            Enter
          </kbd>{" "}
          Open{" "}
          <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
            Esc
          </kbd>{" "}
          Close{" "}
          <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-mono font-semibold text-foreground shadow-sm">
            I
          </kbd>{" "}
          No receipt needed
        </span>
      </div>

      {/* Analytics drill-through filter banner */}
      {hasAnalyticsFilter && (
        <div className="rounded-xl border border-primary/20 bg-indigo-50 p-3 flex items-center gap-3 shadow-warm-sm">
          <div className="flex-shrink-0 h-8 w-8 rounded-full bg-primary/10 flex items-center justify-center">
            <TrendingUp className="h-4 w-4 text-primary" />
          </div>
          <p className="text-sm text-primary font-medium flex-1">
            Filtered by category:{" "}
            <span className="font-semibold">{categoryParam}</span>
            {" · "}
            <span className="font-mono text-xs">
              {startParam} → {endParam}
            </span>
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={clearAnalyticsFilter}
            className="border-primary/30 text-primary hover:bg-primary/5"
          >
            <X className="h-3.5 w-3.5 mr-1.5" />
            Clear filter
          </Button>
        </div>
      )}

      {/* Wise Sync Result Banner */}
      {(wiseSyncResult || syncWiseMutation.isError) && (
        <div className={cn(
          "rounded-xl border p-3 shadow-warm-sm flex items-center gap-3",
          syncWiseMutation.isError
            ? "border-destructive/20 bg-destructive/5"
            : "border-primary/20 bg-primary/5"
        )}>
          {syncWiseMutation.isError ? (
            <AlertTriangle className="h-4 w-4 text-destructive flex-shrink-0" />
          ) : (
            <CheckCircle className="h-4 w-4 text-primary flex-shrink-0" />
          )}
          <p className="text-sm">
            {syncWiseMutation.isError
              ? "Wise sync failed. Check your connection in Settings."
              : wiseSyncResult!.new > 0
                ? `Synced ${wiseSyncResult!.new} new Wise transaction${wiseSyncResult!.new === 1 ? "" : "s"} across ${wiseSyncResult!.months} month${wiseSyncResult!.months === 1 ? "" : "s"} (${wiseSyncResult!.total} total fetched).`
                : `Wise is up to date (${wiseSyncResult!.total} transaction${wiseSyncResult!.total === 1 ? "" : "s"} checked across ${wiseSyncResult!.months} month${wiseSyncResult!.months === 1 ? "" : "s"}, no new ones).`}
          </p>
          <button
            onClick={() => { setWiseSyncResult(null); syncWiseMutation.reset(); }}
            className="ml-auto text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Session-refresh cooldown banner */}
      {rateLimitCountdown > 0 && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-50 dark:bg-amber-950/20 p-4 shadow-warm-sm">
          <div className="flex items-center gap-3">
            <div className="flex-shrink-0 h-8 w-8 rounded-full bg-amber-100 dark:bg-amber-900/30 flex items-center justify-center">
              <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-amber-700 dark:text-amber-300">
                Paused — waiting to renew your session
              </p>
              <p className="text-xs text-amber-600/80 dark:text-amber-400/70 mt-0.5">
                Reconciliation progress is saved. It resumes automatically once the server renews the session.
              </p>
            </div>
            <div className="flex-shrink-0">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 dark:bg-amber-900/40 px-3 py-1 text-sm font-mono font-semibold text-amber-700 dark:text-amber-300 tabular-nums">
                {rateLimitCountdown}s
              </span>
            </div>
          </div>
          <div className="mt-3 h-2 bg-amber-200/50 dark:bg-amber-800/30 rounded-full overflow-hidden">
            <div
              className="h-full bg-amber-400 dark:bg-amber-500 rounded-full transition-all duration-1000 ease-linear"
              style={{ width: `${Math.max((rateLimitCountdown / 60) * 100, 2)}%` }}
            />
          </div>
        </div>
      )}

      {/* Reconciliation Progress / Log Banner */}
      {reconProgress && reconLogs.length > 0 && (
        <div className={cn(
          "rounded-xl border p-4 shadow-warm-sm",
          reconProgress.status === "error"
            ? "border-destructive/20 bg-destructive/5"
            : reconProgress.status === "complete"
            ? "border-primary/20 bg-primary/5"
            : "border-primary/20 bg-primary/5"
        )}>
          <div className="flex items-center gap-3 mb-3">
            <div className={cn(
              "flex-shrink-0 h-8 w-8 rounded-full flex items-center justify-center",
              reconProgress.status === "error" ? "bg-destructive/10" :
              reconProgress.status === "complete" ? "bg-primary/10" : "bg-primary/10"
            )}>
              {(isReconciling || reconSaving) ? (
                <Loader2 className="h-4 w-4 text-primary animate-spin" />
              ) : reconProgress.status === "complete" ? (
                <CheckCircle className="h-4 w-4 text-primary" />
              ) : reconProgress.status === "error" ? (
                <AlertTriangle className="h-4 w-4 text-destructive" />
              ) : (
                <CheckCircle className="h-4 w-4 text-primary" />
              )}
            </div>
            <div className="flex-1 min-w-0">
              <p className={cn(
                "text-sm font-medium",
                reconProgress.status === "error" ? "text-destructive" : "text-primary"
              )}>
                {reconSaving
                  ? "Saving Results..."
                  : reconProgress.status === "complete"
                  ? "Reconciliation Complete"
                  : reconProgress.status === "error"
                  ? "Reconciliation Failed"
                  : reconProgress.phase || "Reconciling..."}
              </p>
              {reconSaving && (
                <p className="text-xs text-primary/70 mt-0.5">
                  Syncing all data before navigation is allowed...
                </p>
              )}
              {isReconciling && reconProgress.detail && (
                <p className="text-xs text-primary/70 mt-0.5">
                  {reconProgress.detail}
                </p>
              )}
              {isReconciling && (reconProgress.pairs_total ?? 0) > 0 && (
                <p
                  data-testid="reconcile-eta"
                  className="text-xs text-primary/70 mt-0.5 tabular-nums"
                >
                  {reconProgress.pairs_done ?? 0} of {reconProgress.pairs_total} done
                  {/* A countdown only while there is something left to count:
                      between two long phases the counts read "n of n", and an
                      ETA there would be a number about nothing. */}
                  {(reconProgress.pairs_done ?? 0) < (reconProgress.pairs_total ?? 0) &&
                    (typeof reconProgress.eta_seconds === "number"
                      ? ` — ${formatReconEta(reconProgress.eta_seconds)}`
                      : " — estimating time left...")}
                  {(reconProgress.llm_calls_done ?? 0) > 0 &&
                    ` · ${reconProgress.llm_calls_done} AI call${reconProgress.llm_calls_done === 1 ? "" : "s"}`}
                </p>
              )}
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
              {(reconProgress.matches_so_far ?? 0) > 0 && (
                <Badge className="bg-primary/10 text-primary border-primary/20 font-mono text-xs">
                  {/* While running this is the engine's candidate count; once
                      complete it is the number of matches actually written. */}
                  {reconProgress.status === "complete"
                    ? `${reconProgress.matches_so_far} new match${reconProgress.matches_so_far === 1 ? "" : "es"}`
                    : `${reconProgress.matches_so_far} matches`}
                </Badge>
              )}
              {isBusy && (
                <span className="text-xs font-mono text-primary/60 tabular-nums">
                  {reconSaving ? "syncing" : `${reconProgress.percent ?? 0}%`}
                </span>
              )}
              {!isBusy && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 rounded-full hover:bg-muted"
                  onClick={() => { setReconProgress(null); setReconLogs([]); }}
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              )}
            </div>
          </div>
          {/* Progress bar (while running or saving) */}
          {isBusy && (
            <div className="h-2.5 bg-primary/10 rounded-full overflow-hidden">
              <div
                className={cn(
                  "h-full bg-primary rounded-full transition-all duration-700 ease-out",
                  reconSaving && "animate-pulse"
                )}
                style={{ width: reconSaving ? "100%" : `${Math.max(reconProgress.percent ?? 0, 2)}%` }}
              />
            </div>
          )}
          {/* Collapsible log window */}
          {reconLogs.length > 0 && (
            <div className={isBusy ? "mt-3" : "mt-2"}>
              <button
                type="button"
                onClick={() => setReconLogsExpanded(!reconLogsExpanded)}
                className="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1 mb-1"
              >
                {reconLogsExpanded ? "Hide details" : "Show details"}
                <span className="text-[10px]">{reconLogsExpanded ? "\u25B2" : "\u25BC"}</span>
              </button>
              {reconLogsExpanded && (
                <div ref={logContainerRef} className={cn(
                  "max-h-48 overflow-y-auto rounded-lg bg-background/80 border p-2 font-mono text-[11px] leading-relaxed",
                  reconProgress.status === "error"
                    ? "border-destructive/10 text-muted-foreground"
                    : "border-primary/10 text-primary/70"
                )}>
                  {reconLogs.map((log, i) => (
                    <div key={i} className={cn(
                      "py-0.5",
                      log.startsWith("ERROR") && "text-destructive font-semibold",
                      log.startsWith("  ") && "pl-4 text-muted-foreground/70"
                    )}>
                      <span className="text-primary/30 mr-2 select-none">{String(i + 1).padStart(2, "0")}</span>
                      {log}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Reconcile Result Banner (only shows after log window is dismissed) */}
      {reconResult && !reconProgress && (
        reconResult.status === "success" || reconResult.status === "partial" ? (
          <div className="rounded-xl border border-primary/20 bg-primary/5 p-4 flex items-center gap-3 shadow-warm-sm">
            <div className="flex-shrink-0 h-8 w-8 rounded-full bg-primary/10 flex items-center justify-center">
              <CheckCircle className="h-4 w-4 text-primary" />
            </div>
            <p className="text-sm text-primary font-medium">
              Reconciliation complete:{" "}
              <span className="font-mono">{reconResult.matched}</span>{" "}
              new match{reconResult.matched === 1 ? "" : "es"} created (
              <span className="font-mono">{Math.round(reconResult.rate)}%</span>{" "}
              reconciled).
              {reconResult.remaining_transactions > 0 && ` ${reconResult.remaining_transactions} still unresolved — link or add proof.`}
              {!!reconResult.refreshed && (
                <>
                  {" "}
                  <span className="font-mono">{reconResult.refreshed}</span>{" "}
                  pair{reconResult.refreshed === 1 ? " was" : "s were"} already on file and
                  only re-scored — not counted as new.
                </>
              )}
              {!!reconResult.flagged_for_review && (
                <>
                  {" "}
                  <span className="font-mono">{reconResult.flagged_for_review}</span>{" "}
                  too uncertain to match — flagged for review instead.
                </>
              )}
              {!!reconResult.displaced && (
                <>
                  {" "}
                  <span className="font-mono">{reconResult.displaced}</span>{" "}
                  weak match{reconResult.displaced === 1 ? "" : "es"} replaced by a better one.
                </>
              )}
              {!!reconResult.llm_calls && (
                <>
                  {" "}
                  AI agent calls:{" "}
                  <span className="font-mono">{reconResult.llm_calls}</span>, of which{" "}
                  <span className="font-mono">{reconResult.llm_fallbacks ?? 0}</span>{" "}
                  fell back to deterministic scoring.
                </>
              )}
            </p>
          </div>
        ) : reconResult.status === "no_matches" ? (
          <div className="rounded-xl border border-orange-500/20 bg-orange-500/5 p-4 flex items-center gap-3 shadow-warm-sm">
            <div className="flex-shrink-0 h-8 w-8 rounded-full bg-orange-500/10 flex items-center justify-center">
              <AlertTriangle className="h-4 w-4 text-orange-600" />
            </div>
            <p className="text-sm text-orange-700 dark:text-orange-300 font-medium">
              {reconResult.refreshed
                ? `No new matches. ${reconResult.refreshed} pair${reconResult.refreshed === 1 ? "" : "s"} already on file ${reconResult.refreshed === 1 ? "was" : "were"} re-scored and ${reconResult.refreshed === 1 ? "is" : "are"} waiting in Review.`
                : "No matches found. Upload receipts that correspond to your bank transactions, then run Match Receipts again."}
            </p>
          </div>
        ) : (
          <div className="rounded-xl border border-muted-foreground/20 bg-muted/50 p-4 flex items-center gap-3 shadow-warm-sm">
            <div className="flex-shrink-0 h-8 w-8 rounded-full bg-muted flex items-center justify-center">
              <CheckCircle className="h-4 w-4 text-muted-foreground" />
            </div>
            <p className="text-sm text-muted-foreground font-medium">
              Nothing to match yet. Upload bank statements and receipts first.
            </p>
          </div>
        )
      )}

      {/* Pending Review Banner — hidden while reconciliation is saving */}
      {pendingCount > 0 && !reconSaving && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/80 p-4 flex items-center gap-3 shadow-warm-sm dark:bg-amber-950/20 dark:border-amber-800">
          <div className="flex-shrink-0 h-8 w-8 rounded-full bg-amber-100 flex items-center justify-center dark:bg-amber-900/40">
            <AlertTriangle className="h-4 w-4 text-amber-600" />
          </div>
          <p className="text-sm text-amber-800 dark:text-amber-200 font-medium flex-1">
            {pendingCount} transaction{pendingCount !== 1 ? "s" : ""} pending
            review.
          </p>
          <Button
            size="sm"
            onClick={handleTakeAction}
            disabled={navigatingToReview || isBusy}
            className="bg-amber-600 hover:bg-amber-700 text-white shadow-sm font-semibold px-4"
          >
            {navigatingToReview ? (
              <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
            ) : null}
            {navigatingToReview ? "Loading..." : "Take Action"}
            {!navigatingToReview && <ArrowRight className="h-4 w-4 ml-1.5" />}
          </Button>
        </div>
      )}

      {/* Upload Zones */}
      <UploadZone open={statementUploadOpen} onClose={() => setStatementUploadOpen(false)} mode="statement" />
      <UploadZone id="upload-zone" open={uploadOpen} onClose={() => setUploadOpen(false)} mode={uploadMode} />

      {/* Processing queue. Deliberately outside the upload zones: a bulk run
          outlives the drop target by a long way, and closing the zone must not
          take away the only readout of what the server is doing with it. One
          queue for every file, so nothing can show a second, disagreeing
          "parsing page X of Y" for whichever statement emitted an event last. */}
      <ProcessingQueuePanel
        id="processing-queue"
        alwaysShow={queuePanelForced}
      />

      {/* Persistent Uploaded Files Section */}
      {(sourceFiles.length > 0 || uploadedDocuments.length > 0) && (
        <div className="rounded-xl border border-border/60 bg-card p-4 shadow-warm-sm space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-foreground">
              Uploaded Files
            </h3>
            <span className="text-xs text-muted-foreground">
              {sourceFiles.length} statement{sourceFiles.length !== 1 ? "s" : ""}
              {totalDocuments > 0 && ` · ${totalDocuments} proof${totalDocuments !== 1 ? "s" : ""}`}
            </span>
          </div>

          {/* Statements */}
          {sourceFiles.length > 0 && (
            <div className="space-y-1.5">
              <div className="flex items-center gap-2 flex-wrap">
                <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                  Statements ({filteredSourceFiles.length}{filteredSourceFiles.length !== sourceFiles.length ? `/${sourceFiles.length}` : ""})
                </p>
                <div className="flex items-center gap-1.5 ml-auto">
                  <label className="text-[10px] text-muted-foreground">From</label>
                  <input
                    type="date"
                    value={statementsDateFrom}
                    onChange={(e) => setStatementsDateFrom(e.target.value)}
                    className="h-6 px-1.5 text-[10px] rounded border border-border/60 bg-background text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
                  />
                  <label className="text-[10px] text-muted-foreground">To</label>
                  <input
                    type="date"
                    value={statementsDateTo}
                    onChange={(e) => setStatementsDateTo(e.target.value)}
                    className="h-6 px-1.5 text-[10px] rounded border border-border/60 bg-background text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
                  />
                  <button
                    type="button"
                    disabled={clearAllStatementsMutation.isPending}
                    onClick={() => setClearStatementsDialog({ open: true, step: 1 })}
                    className="h-6 px-2 text-[10px] font-semibold rounded border border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive hover:text-destructive-foreground transition-colors disabled:opacity-50 cursor-pointer flex items-center gap-1"
                    title="Delete all statements and their transactions"
                  >
                    {clearAllStatementsMutation.isPending ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Trash2 className="h-3 w-3" />
                    )}
                    Clear All
                  </button>
                </div>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-1.5 max-h-[280px] overflow-y-auto">
                {filteredSourceFiles.map((sf) => (
                  <div
                    key={sf.source_file}
                    className="flex items-center gap-2 rounded-lg border border-border/40 bg-muted/20 p-2.5 group"
                  >
                    <button
                      className="flex-shrink-0 h-7 w-7 rounded-md bg-primary/5 flex items-center justify-center hover:bg-primary/15 transition-colors cursor-pointer"
                      title="Open statement file"
                      onClick={() => viewFile(`/api/transactions/statement-file?source_file=${encodeURIComponent(sf.source_file)}`)}
                    >
                      <FileText className="h-3 w-3 text-primary/50" />
                    </button>
                    <div
                      className="flex-1 min-w-0 cursor-pointer"
                      onClick={() => handleStatementTab(sf.source_file)}
                    >
                      <p className="text-xs font-medium truncate">{sf.source_file}</p>
                      <p className="text-[10px] text-muted-foreground">
                        {accountNames[sf.account] || sf.account} · {sf.transaction_count} transactions
                      </p>
                    </div>
                    <Badge className="text-[10px] bg-primary/5 text-primary/60 border-primary/10 shrink-0">
                      Imported
                    </Badge>
                    <button
                      className={cn(
                        "flex-shrink-0 h-5 w-5 rounded-full flex items-center justify-center transition-all cursor-pointer",
                        deleteStatementMutation.isPending && deleteStatementMutation.variables === sf.source_file
                          ? "opacity-100"
                          : "opacity-0 group-hover:opacity-100 hover:bg-destructive/10"
                      )}
                      title="Delete statement and its transactions"
                      disabled={deleteStatementMutation.isPending && deleteStatementMutation.variables === sf.source_file}
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteStatementTarget(sf.source_file);
                      }}
                    >
                      {deleteStatementMutation.isPending && deleteStatementMutation.variables === sf.source_file ? (
                        <Loader2 className="h-3 w-3 text-destructive/60 animate-spin" />
                      ) : (
                        <Trash2 className="h-3 w-3 text-destructive/60" />
                      )}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Receipts & Invoices */}
          {uploadedDocuments.length > 0 && (
            <div className="space-y-1.5">
              <div className="flex items-center gap-2 flex-wrap">
                <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                  Receipts & Invoices ({filteredDocuments.length}{filteredDocuments.length !== totalDocuments ? `/${totalDocuments}` : ""})
                </p>
                <div className="flex items-center gap-1.5">
                  <label className="text-[10px] text-muted-foreground">From</label>
                  <input
                    type="date"
                    value={proofsDateFrom}
                    onChange={(e) => setProofsDateFrom(e.target.value)}
                    className="h-6 px-1.5 text-[10px] rounded border border-border/60 bg-background text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
                  />
                  <label className="text-[10px] text-muted-foreground">To</label>
                  <input
                    type="date"
                    value={proofsDateTo}
                    onChange={(e) => setProofsDateTo(e.target.value)}
                    className="h-6 px-1.5 text-[10px] rounded border border-border/60 bg-background text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
                  />
                </div>
                <div className="flex items-center gap-1 ml-auto">
                  {(["all", "MATCHED", "EXTRACTED"] as const).map((f) => (
                    <button
                      key={f}
                      onClick={() => setProofFilter(f)}
                      className={cn(
                        "px-2.5 py-0.5 rounded-full text-[10px] font-medium transition-all",
                        proofFilter === f
                          ? "bg-primary text-primary-foreground shadow-warm-sm"
                          : "bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground"
                      )}
                    >
                      {f === "all" ? "All" : f === "MATCHED" ? "Matched" : "Ready"}
                    </button>
                  ))}
                  <button
                    type="button"
                    disabled={clearAllDocumentsMutation.isPending}
                    onClick={() => setClearReceiptsDialog({ open: true, step: 1 })}
                    className="h-6 px-2 text-[10px] font-semibold rounded border border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive hover:text-destructive-foreground transition-colors disabled:opacity-50 cursor-pointer flex items-center gap-1 ml-1"
                    title="Delete all receipts"
                  >
                    {clearAllDocumentsMutation.isPending ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Trash2 className="h-3 w-3" />
                    )}
                    Clear All
                  </button>
                </div>
              </div>
              <input
                type="text"
                value={proofSearch}
                onChange={(e) => setProofSearch(e.target.value)}
                placeholder="Search by vendor, filename, amount, or receipt ID…"
                className="h-7 px-2 text-xs rounded border border-border/60 bg-background text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30 w-full"
              />
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-1.5 max-h-[280px] overflow-y-auto">
                {filteredDocuments.map((doc) => (
                  <div
                    key={doc.id}
                    className="flex items-center gap-2 rounded-lg border border-border/40 bg-muted/20 p-2.5 group"
                  >
                    <button
                      className="flex-shrink-0 h-7 w-7 rounded-md bg-primary/5 flex items-center justify-center hover:bg-primary/15 transition-colors cursor-pointer"
                      title="View document"
                      onClick={() => viewFile(`/api/receipts/${doc.id}/download`)}
                    >
                      <ExternalLink className="h-3 w-3 text-primary/50" />
                    </button>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-medium truncate">
                        <span className="font-mono text-muted-foreground/70 mr-1.5">#{doc.id}</span>
                        {doc.filename}
                      </p>
                      <p className="text-[10px] text-muted-foreground">
                        {doc.vendor ?? "Unknown"}{doc.date ? ` · ${doc.date}` : ""}
                        {doc.total != null ? ` · $${Number(doc.total).toFixed(2)}` : ""}
                      </p>
                    </div>
                    <ProofStatusBadge
                      docId={doc.id}
                      status={doc.status}
                      linkedTxnId={doc.matched_transaction_id}
                      linkedTxnCount={doc.matched_transaction_count}
                      linkedSourceFile={doc.matched_source_file}
                      onJump={jumpToLinkedTransaction}
                    />
                    <button
                      className={cn(
                        "flex-shrink-0 h-5 w-5 rounded-full flex items-center justify-center transition-all cursor-pointer",
                        deleteDocumentMutation.isPending && deleteDocumentMutation.variables === doc.id
                          ? "opacity-100"
                          : "opacity-0 group-hover:opacity-100 hover:bg-destructive/10"
                      )}
                      title="Delete receipt"
                      disabled={deleteDocumentMutation.isPending && deleteDocumentMutation.variables === doc.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteDocumentTarget(doc.id);
                      }}
                    >
                      {deleteDocumentMutation.isPending && deleteDocumentMutation.variables === doc.id ? (
                        <Loader2 className="h-3 w-3 text-destructive/60 animate-spin" />
                      ) : (
                        <Trash2 className="h-3 w-3 text-destructive/60" />
                      )}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* A statement with no tab of its own — every row it carried was already
          in the books, so nothing in `transactions` carries its name. Say so
          rather than selecting a different statement's tab in silence. */}
      {tablessStatement && (
        <div className="rounded-xl border border-primary/20 bg-primary/5 px-3 py-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{tablessStatement}</span>{" "}
          added no new transactions — every row it carried was already in your
          books, so it has no tab of its own. Its rows are on the statement that
          first imported them; the list below shows every statement you have
          uploaded.
        </div>
      )}

      {/* Bank Statement Tabs */}
      {sourceFiles.length > 0 ? (
        <Tabs value={activeSourceFile ?? sourceFiles[0].source_file} onValueChange={handleStatementTab}>
          <TabsList className="flex flex-wrap h-auto gap-1 bg-muted/50 p-1.5 rounded-xl">
            {sourceFiles.map((sf) => (
              <TabsTrigger
                key={sf.source_file}
                value={sf.source_file}
                title={sf.source_file}
                className={cn(
                  "px-4 py-2 text-sm rounded-lg",
                  "data-[state=active]:bg-primary data-[state=active]:text-primary-foreground data-[state=active]:shadow-warm-sm",
                  sf.transaction_count === 0 && "opacity-50"
                )}
              >
                {formatTabLabel(sf, accountNames)}
                <span className="ml-1.5 font-mono text-[10px] opacity-70">
                  ({sf.transaction_count})
                </span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      ) : (
        <div className="rounded-xl bg-muted/30 border border-dashed border-border/60 p-3 text-center text-sm text-muted-foreground">
          No statements uploaded. Click "Upload Statements" to get started.
        </div>
      )}

      {/* Search crosses statement tabs — tell the user so results from
          other statements don't look like ghosts of the selected tab. */}
      {hasSearchFilter && (
        <div className="flex items-center gap-2 rounded-lg bg-primary/5 border border-primary/20 px-3 py-2 text-xs text-primary">
          <Search className="h-3.5 w-3.5 shrink-0" />
          Searching across all statements — results may come from statements
          other than the selected tab.
        </div>
      )}

      {/* Filters */}
      <TransactionFilters
        filters={filters}
        onFiltersChange={handleFiltersChange}
        dateRange={dateRangeData}
        availableAccounts={[...new Set(sourceFiles.map((f) => f.account))]}
        rightActions={
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                if (activeSourceFile && !isBusy) {
                  startReconciliation("statement", activeSourceFile);
                }
              }}
              disabled={isBusy || !activeSourceFile}
              className="h-9 rounded-full gap-1.5 border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
              title={
                activeStatement
                  ? `Match only the transactions in "${formatTabLabel(activeStatement, accountNames)}" against all unmatched receipts`
                  : "Select a statement tab to match it"
              }
            >
              {isBusy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )}
              Match This Statement
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setRecategorizeModalOpen(true);
                if (!recategorizing && !recategorizeMutation.isPending) {
                  recategorizeMutation.mutate();
                }
              }}
              disabled={recategorizeMutation.isPending}
              className="h-9 rounded-full gap-1.5 border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50"
              title="Run the categorization agent across every transaction (user-confirmed rows stay locked)"
            >
              {recategorizing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Sparkles className="h-3.5 w-3.5" />
              )}
              {recategorizing
                ? recategorizeProgress && (recategorizeProgress.total ?? 0) > 0
                  ? `Recategorizing ${recategorizeProgress.done ?? 0}/${recategorizeProgress.total}...`
                  : "Recategorizing..."
                : "Recategorize"}
            </Button>
          </>
        }
      />

      {/* Chain navigation breadcrumb */}
      {chainBreadcrumbs.length > 0 && (
        <ChainBreadcrumb
          entries={chainBreadcrumbs}
          onNavigateBack={() => {
            const prev = chainBreadcrumbs[chainBreadcrumbs.length - 1];
            if (prev) {
              setChainBreadcrumbs((b) => b.slice(0, -1));
              setSearchParams((sp) =>
                withParams(sp, {
                  statement: prev.source_file,
                  highlight: String(prev.transaction_id),
                  page: null,
                }),
              );
            }
          }}
          onDismiss={() => setChainBreadcrumbs([])}
        />
      )}

      {/* Table */}
      <Card className="shadow-warm rounded-xl overflow-hidden border-border/60">
        <CardContent className="p-0">
          <div className="relative">
            {/* Stale-data overlay during tab switch or refetch */}
            {(isPlaceholderData || (isFetching && !isLoading)) && (
              <div className="absolute inset-0 bg-background/40 z-10 flex items-center justify-center rounded-xl">
                <div className="flex items-center gap-2 bg-card px-4 py-2 rounded-full shadow-warm-sm border">
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                  <span className="text-sm text-muted-foreground">
                    {isPlaceholderData ? "Switching..." : "Refreshing..."}
                  </span>
                </div>
              </div>
            )}
            <TransactionTable
              transactions={filteredTransactions}
              isLoading={isLoading}
              onRowClick={handleRowClick}
              selectedId={selectedTransaction?.id ?? null}
              sort={sort}
              onSortChange={(s) =>
                setSearchParams((prev) =>
                  withParams(prev, {
                    sort: s.column ? `${s.column}:${s.direction}` : null,
                    page: null,
                  }),
                )
              }
              onNoteClick={(txn) => setNoteTransaction(txn)}
              onProofClick={handleProofClick}
              onProofFileClick={(_txn, docId) => setProofViewerDocId(docId)}
              onLinkTransactionClick={handleLinkTransactionClick}
              onCategoryChange={handleCategoryChange}
              onToggleIgnore={handleToggleIgnore}
              categories={expenseCategories}
              categoryGroups={categoryGroups}
              highlightId={highlightId}
              focusedId={focusedTxnId}
              selectedIds={selectedTxnIds}
              onSelectionChange={setSelectedTxnIds}
              onBatchAction={(action, ids, extra) => {
                if (action === "ignore") {
                  // The endpoint is a toggle, so only fire it on rows that
                  // aren't already marked — otherwise a bulk "No receipt
                  // needed" over a mixed selection would silently flip the
                  // already-marked rows back to needing one.
                  ids.forEach((id) => {
                    const txn = txnData?.transactions.find((t) => String(t.id) === String(id));
                    if (txn && txn.status !== "IGNORED") handleToggleIgnore(txn);
                  });
                  setSelectedTxnIds(new Set());
                } else if (action === "categorize" && extra) {
                  ids.forEach((id) => {
                    const txn = txnData?.transactions.find((t) => String(t.id) === String(id));
                    if (txn) handleCategoryChange(txn, extra);
                  });
                  setSelectedTxnIds(new Set());
                }
              }}
            />
          </div>
          {txnData && (
            <Pagination
              page={txnData.page}
              perPage={txnData.per_page}
              total={txnData.total}
              onPageChange={setPage}
            />
          )}
        </CardContent>
      </Card>

      {/* Receipt Slide-Over */}
      <ReceiptSlideOver
        transaction={selectedTransaction}
        open={slideOverOpen}
        onOpenChange={(open) => {
          setSlideOverOpen(open);
          if (!open) setSelectedTransaction(null);
        }}
        onMatchManually={(txn) => {
          setSlideOverOpen(false);
          handleProofClick(txn);
        }}
        onUnmatch={() => {
          // ReceiptSlideOver handles the actual removal internally via
          // useRemoveProof (which invalidates the relevant queries) and
          // closes itself. Nothing else to do here: re-opening the match
          // dialog would undo the unmatch the user just asked for.
          setSlideOverOpen(false);
        }}
        onRematch={(txn) => {
          setSlideOverOpen(false);
          handleProofClick(txn);
        }}
        onLinkTransaction={handleLinkTransactionClick}
        onChainNavigate={handleChainNavigate}
        onAttachAnother={handleAttachAnother}
        onCategoryChange={handleCategoryChange}
        categories={expenseCategories}
        categoryGroups={categoryGroups}
      />

      {/* Receipt Selector Dialog (single match) */}
      <ReceiptSelectorDialog
        open={matchDialogOpen}
        onOpenChange={setMatchDialogOpen}
        transaction={matchDialogTransaction}
        existingMatchId={matchDialogMatchId}
        onMatchSuccess={handleMatchSuccess}
      />

      {/* Proof document viewer — opened by clicking a proof filename in the table */}
      <ReceiptViewerModal
        documentId={proofViewerDocId}
        open={proofViewerDocId !== null}
        onOpenChange={(open) => {
          if (!open) setProofViewerDocId(null);
        }}
      />

      {/* Receipt Selector Dialog (multi-proof attach) */}
      <ReceiptSelectorDialog
        open={multiMatchOpen}
        onOpenChange={(open) => {
          if (addProof.isPending) return;
          setMultiMatchOpen(open);
          if (!open) setMultiMatchTransaction(null);
        }}
        transaction={multiMatchTransaction}
        onMatchSuccess={() => {
          setMultiMatchOpen(false);
          setMultiMatchTransaction(null);
        }}
        mode="multi"
        onMultiMatchSuccess={handleMultiMatchSuccess}
        isExternalPending={addProof.isPending}
      />

      {/* Transaction Link Dialog */}
      <TransactionLinkDialog
        open={linkDialogOpen}
        onOpenChange={setLinkDialogOpen}
        transaction={linkDialogTransaction}
        onLinkSuccess={() => {
          setLinkDialogOpen(false);
          setLinkDialogTransaction(null);
        }}
      />

      {/* Note Panel */}
      <NotePanel
        open={noteTransaction !== null}
        onClose={() => setNoteTransaction(null)}
        transactionId={noteTransaction?.id ?? null}
        transactionDescription={noteTransaction?.description ?? null}
        currentNote={noteTransaction?.note ?? null}
      />

      {/* Wise Reconnect Dialog — the stored token can no longer be decrypted */}
      <Dialog
        open={wiseReconnectOpen}
        onOpenChange={(open) => {
          setWiseReconnectOpen(open);
          if (!open) {
            setWiseReconnectToken("");
            setWiseReconnectError("");
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Reconnect Wise</DialogTitle>
            <DialogDescription>
              Your saved Wise token can't be read any more. This happens when the server's
              encryption secret changes. Paste a read-only Wise API token to keep syncing
              statements.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label
                htmlFor="wise-reconnect-token"
                className="text-sm font-medium leading-none"
              >
                API token
              </label>
              <Input
                id="wise-reconnect-token"
                type="password"
                className="font-mono"
                placeholder="Paste your Wise API token"
                value={wiseReconnectToken}
                onChange={(e) => {
                  setWiseReconnectToken(e.target.value);
                  setWiseReconnectError("");
                }}
              />
              {wiseReconnectError && (
                <p className="flex items-center gap-1.5 text-destructive text-sm">
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                  {wiseReconnectError}
                </p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button
                onClick={() => reconnectWiseMutation.mutate()}
                disabled={!wiseReconnectToken.trim() || reconnectWiseMutation.isPending}
                className="bg-primary hover:bg-primary/90 text-primary-foreground"
              >
                {reconnectWiseMutation.isPending && (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                )}
                Reconnect and sync
              </Button>
              <Button
                variant="ghost"
                onClick={() => setWiseWalkthroughOpen(true)}
                className="text-muted-foreground"
              >
                Need help?
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Wise token walkthrough — the guided route to a fresh read-only token */}
      <WiseTokenWalkthrough
        open={wiseWalkthroughOpen}
        onOpenChange={setWiseWalkthroughOpen}
        onSuccess={() => handleWiseReconnected()}
      />

      {/* Wise Profile Selector Dialog */}
      <Dialog open={wiseProfileDialogOpen} onOpenChange={setWiseProfileDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Sync Wise Transactions</DialogTitle>
            <DialogDescription>
              Choose which Wise account to sync and the date range to import.
            </DialogDescription>
          </DialogHeader>
          {wiseProfilesLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            </div>
          ) : (
            <div className="space-y-4">
              {/* Profile list */}
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wide">
                  Wise Account
                </p>
                <div className="space-y-2 max-h-[220px] overflow-y-auto">
                  {wiseProfiles.map((profile) => (
                    <button
                      key={profile.id}
                      disabled={selectWiseProfile.isPending}
                      onClick={() => setWiseSelectedProfileId(profile.id)}
                      className={cn(
                        "w-full text-left rounded-lg border p-3 transition-all hover:border-primary/50 hover:bg-primary/5",
                        wiseSelectedProfileId === profile.id
                          ? "border-primary bg-primary/5"
                          : "border-border"
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <div>
                          <p className="text-sm font-medium">{profile.name}</p>
                          <p className="text-xs text-muted-foreground capitalize">
                            {profile.type} profile
                          </p>
                        </div>
                        {wiseSelectedProfileId === profile.id && (
                          <Badge className="bg-primary/10 text-primary border-primary/20 text-[10px]">
                            Selected
                          </Badge>
                        )}
                      </div>
                      {profile.balances.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {profile.balances.map((bal) => (
                            <span
                              key={bal.currency}
                              className="text-[11px] font-mono bg-muted/50 px-1.5 py-0.5 rounded"
                            >
                              {bal.currency} {bal.amount.toLocaleString(undefined, {
                                minimumFractionDigits: 2,
                                maximumFractionDigits: 2,
                              })}
                            </span>
                          ))}
                        </div>
                      )}
                    </button>
                  ))}
                </div>
              </div>

              {/* Date range picker */}
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wide">
                  Date Range
                </p>
                <div className="flex items-center gap-2">
                  <div className="flex-1">
                    <label className="text-xs text-muted-foreground block mb-1">From</label>
                    <input
                      type="date"
                      value={wiseSyncSince}
                      max={wiseSyncUntil}
                      onChange={(e) => setWiseSyncSince(e.target.value)}
                      className="w-full h-9 rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
                    />
                  </div>
                  <div className="flex-1">
                    <label className="text-xs text-muted-foreground block mb-1">To</label>
                    <input
                      type="date"
                      value={wiseSyncUntil}
                      min={wiseSyncSince}
                      max={new Date().toISOString().slice(0, 10)}
                      onChange={(e) => setWiseSyncUntil(e.target.value)}
                      className="w-full h-9 rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
                    />
                  </div>
                </div>
              </div>

              {/* Action button */}
              <Button
                className="w-full bg-primary hover:bg-primary/90 text-primary-foreground"
                disabled={!wiseSelectedProfileId || selectWiseProfile.isPending}
                onClick={() => {
                  if (wiseSelectedProfileId) {
                    selectWiseProfile.mutate(wiseSelectedProfileId);
                  }
                }}
              >
                {selectWiseProfile.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Syncing...
                  </>
                ) : (
                  <>
                    <ArrowLeftRight className="h-4 w-4 mr-2" />
                    Sync Wise Transactions
                  </>
                )}
              </Button>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Amazon import walkthrough */}
      <AmazonImportWalkthrough
        open={amazonImportOpen}
        onOpenChange={setAmazonImportOpen}
        onSuccess={() => {
          queryClient.invalidateQueries({ queryKey: ["transactions"] });
          queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
          queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
        }}
      />

      {/* Categorization agent modal — live progress log + final summary */}
      <Dialog
        open={recategorizeModalOpen}
        onOpenChange={(open) => {
          // Block closing while the agent is still running.
          if (!open && recategorizing) return;
          setRecategorizeModalOpen(open);
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Categorization Agent</DialogTitle>
          </DialogHeader>
          <ProgressLogPanel
            status={
              recategorizing
                ? "running"
                : recategorizeProgress?.status === "error"
                ? "error"
                : recategorizeProgress?.status === "complete"
                ? "complete"
                : "idle"
            }
            phase={recategorizeProgress?.phase ?? (recategorizing ? "Starting..." : undefined)}
            detail={recategorizeProgress?.error ?? undefined}
            percent={recategorizeProgress?.percent ?? 0}
            logs={recategorizeProgress?.logs ?? []}
            maxHeightClass="max-h-80"
            onClose={() => setRecategorizeModalOpen(false)}
          />
          {recategorizeProgress?.summary && !recategorizing && (
            <div className="rounded-xl border border-primary/10 bg-primary/5 p-4 text-sm">
              <div className="font-medium text-primary mb-2">Run summary</div>
              <div className="grid grid-cols-2 gap-y-1 text-xs text-muted-foreground">
                <div>Total transactions</div>
                <div className="font-mono text-right">
                  {recategorizeProgress.summary.total ?? 0}
                </div>
                <div>Rule-locked</div>
                <div className="font-mono text-right">
                  {recategorizeProgress.summary.rule_locked ?? 0}
                </div>
                <div>Vendors categorized</div>
                <div className="font-mono text-right">
                  {recategorizeProgress.summary.vendors_categorized ?? 0}
                </div>
                <div>Mixed-use refinements</div>
                <div className="font-mono text-right">
                  {recategorizeProgress.summary.txns_refined ?? 0}
                </div>
                <div>Categories in taxonomy</div>
                <div className="font-mono text-right">
                  {recategorizeProgress.summary.categories_final ?? 0}
                </div>
                <div>LLM calls used</div>
                <div className="font-mono text-right">
                  {recategorizeProgress.summary.llm_calls ?? 0}
                </div>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Clear All Statements — two-step confirm dialog */}
      <ConfirmDialog
        open={clearStatementsDialog.open}
        onOpenChange={(open) => {
          if (!open) setClearStatementsDialog({ open: false, step: 1 });
        }}
        title={
          clearStatementsDialog.step === 1
            ? `Clear all ${sourceFiles.length} statement${sourceFiles.length !== 1 ? "s" : ""}?`
            : "Are you absolutely sure?"
        }
        description={
          clearStatementsDialog.step === 1
            ? "This will delete every statement and every transaction they contain. This cannot be undone."
            : "This will permanently delete every uploaded statement, wipe all their transactions, and reset matched proofs back to unmatched."
        }
        confirmLabel={clearStatementsDialog.step === 1 ? "Continue" : "Delete Everything"}
        variant="destructive"
        isPending={clearAllStatementsMutation.isPending}
        onConfirm={() => {
          if (clearStatementsDialog.step === 1) {
            setClearStatementsDialog({ open: true, step: 2 });
          } else {
            clearAllStatementsMutation.mutate(undefined, {
              onSettled: () => setClearStatementsDialog({ open: false, step: 1 }),
            });
          }
        }}
      />

      {/* Delete Single Statement — confirm dialog */}
      <ConfirmDialog
        open={deleteStatementTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteStatementTarget(null);
        }}
        title="Delete statement?"
        description={
          deleteStatementTarget
            ? `Delete "${deleteStatementTarget}" and all its transactions? This cannot be undone.`
            : ""
        }
        confirmLabel="Delete"
        variant="destructive"
        isPending={deleteStatementMutation.isPending}
        onConfirm={() => {
          if (deleteStatementTarget) {
            deleteStatementMutation.mutate(deleteStatementTarget, {
              onSettled: () => setDeleteStatementTarget(null),
            });
          }
        }}
      />

      {/* Clear All Receipts — two-step confirm dialog */}
      <ConfirmDialog
        open={clearReceiptsDialog.open}
        onOpenChange={(open) => {
          if (!open) setClearReceiptsDialog({ open: false, step: 1 });
        }}
        title={
          clearReceiptsDialog.step === 1
            ? `Clear all ${totalDocuments} receipt${totalDocuments !== 1 ? "s" : ""}?`
            : "Are you absolutely sure?"
        }
        description={
          clearReceiptsDialog.step === 1
            ? "This will delete every uploaded receipt. This cannot be undone."
            : "This will permanently delete every uploaded receipt and remove their matches."
        }
        confirmLabel={clearReceiptsDialog.step === 1 ? "Continue" : "Delete Everything"}
        variant="destructive"
        isPending={clearAllDocumentsMutation.isPending}
        onConfirm={() => {
          if (clearReceiptsDialog.step === 1) {
            setClearReceiptsDialog({ open: true, step: 2 });
          } else {
            clearAllDocumentsMutation.mutate(undefined, {
              onSettled: () => setClearReceiptsDialog({ open: false, step: 1 }),
            });
          }
        }}
      />

      {/* Delete Single Document — confirm dialog */}
      <ConfirmDialog
        open={deleteDocumentTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteDocumentTarget(null);
        }}
        title="Delete receipt?"
        description={
          deleteDocumentTarget
            ? `Delete this receipt? This cannot be undone.`
            : ""
        }
        confirmLabel="Delete"
        variant="destructive"
        isPending={deleteDocumentMutation.isPending}
        onConfirm={() => {
          if (deleteDocumentTarget) {
            deleteDocumentMutation.mutate(deleteDocumentTarget, {
              onSettled: () => setDeleteDocumentTarget(null),
            });
          }
        }}
      />

    </div>
  );
}
