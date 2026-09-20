import { useState, useEffect, useMemo, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  FileText,
  Search,
  Loader2,
  Unlink,
  CheckCircle,
  Star,
  ArrowLeftRight,
  Ban,
  Calendar,
  DollarSign,
  Building2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { Transaction } from "./TransactionTable";
import { CoverageIndicator } from "./CoverageIndicator";

interface MatchCandidate {
  id: number;
  filename: string;
  vendor: string | null;
  date: string | null;
  total: string | null;
  currency: string | null;
  confidence_score: number;
  amount_score: number;
  date_score: number;
  vendor_score: number;
  // Many-to-many: receipt may already cover other transactions (split payment).
  is_already_matched?: boolean;
  existing_match_count?: number;
  // The user rejected this receipt for this transaction before (or rejected an
  // identical upload of it). Still selectable — this is where a human may
  // overrule their own earlier call — but it says so.
  previously_rejected?: boolean;
}

interface AvailableMatchesResponse {
  candidates: MatchCandidate[];
  transaction: {
    id: number;
    amount: string;
    currency: string;
    date: string;
    description: string;
  };
  total: number;
}

interface ReceiptSelectorDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  transaction: Transaction | null;
  existingMatchId?: number | null;
  onMatchSuccess: () => void;
  mode?: "single" | "multi";
  onMultiMatchSuccess?: (docIds: number[]) => void;
  isExternalPending?: boolean;
}

function formatCurrency(amount: string | null, currency: string | null): string {
  if (!amount) return "--";
  const num = parseFloat(amount);
  if (isNaN(num)) return "--";
  return num.toLocaleString("en-CA", {
    style: "currency",
    currency: currency === "CAD" || currency === "USD" ? currency : "CAD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "No date";
  try {
    const d = new Date(dateStr + "T00:00:00");
    return d.toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return dateStr;
  }
}

/* ── Confidence badge with color coding ────────────────────────── */

function ConfidenceScore({ score }: { score: number }) {
  const percent = Math.round(score * 100);
  let bgClass: string;
  let textClass: string;

  if (percent >= 80) {
    bgClass = "bg-success";
    textClass = "text-white";
  } else if (percent >= 60) {
    bgClass = "bg-amber-500";
    textClass = "text-white";
  } else if (percent >= 40) {
    bgClass = "bg-amber-300";
    textClass = "text-amber-900";
  } else {
    bgClass = "bg-rose-500";
    textClass = "text-white";
  }

  return (
    <span
      className={cn(
        "inline-flex items-center justify-center rounded-md px-2 py-0.5 text-[11px] font-bold font-mono",
        bgClass,
        textClass
      )}
    >
      {percent}%
    </span>
  );
}

/* ── Transaction header card (the item being matched) ──────────── */

function TransactionHeader({ transaction }: { transaction: Transaction }) {
  const absAmount = Math.abs(transaction.amount);
  const isCredit = transaction.amount > 0;

  return (
    <div className="rounded-xl border-2 border-primary/20 bg-primary/[0.03] p-4">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-primary mb-2">
        Transaction to match
      </p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2">
        <div className="col-span-2">
          <p className="text-sm font-semibold text-foreground leading-tight">
            {transaction.description}
          </p>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <DollarSign className="h-3 w-3 shrink-0" />
          <span className="font-mono font-semibold text-foreground">
            {isCredit ? "+" : "-"}{formatCurrency(String(absAmount), transaction.currency)}
          </span>
          <span className="text-muted-foreground">{transaction.currency}</span>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Calendar className="h-3 w-3 shrink-0" />
          <span>{formatDate(transaction.date)}</span>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Building2 className="h-3 w-3 shrink-0" />
          <span>{transaction.account}</span>
        </div>
        {transaction.category && (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <ArrowLeftRight className="h-3 w-3 shrink-0" />
            <span>{transaction.category}</span>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Candidate receipt row ──────────────────────────────────────── */

function CandidateRow({
  doc,
  selected,
  onClick,
  isBestMatch,
}: {
  doc: MatchCandidate;
  selected: boolean;
  onClick: () => void;
  isBestMatch?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full relative flex items-start gap-3 p-3 rounded-lg border transition-all text-left",
        selected
          ? "border-primary bg-primary/5 ring-2 ring-primary/20"
          : "border-border/40 hover:border-border hover:bg-muted/30"
      )}
      aria-selected={selected}
      role="option"
    >
      <FileText className="h-5 w-5 text-muted-foreground flex-shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0 space-y-1">
        {/* Row 1: vendor + receipt ID + best match badge */}
        <div className="flex items-center gap-1.5">
          <span className="font-semibold text-sm truncate text-foreground">
            {doc.vendor || "Unknown Vendor"}
          </span>
          <span className="font-mono text-[10px] text-muted-foreground/70 shrink-0">
            #{doc.id}
          </span>
          {isBestMatch && (
            <Badge className="bg-primary/10 text-primary border-primary/20 text-[9px] px-1.5 py-0 gap-0.5 shrink-0">
              <Star className="h-2.5 w-2.5" />
              Best
            </Badge>
          )}
          {doc.is_already_matched && (
            <Badge
              className="bg-amber-100 text-amber-900 border-amber-200 text-[9px] px-1.5 py-0 gap-0.5 shrink-0"
              title={`Already linked to ${doc.existing_match_count ?? 1} transaction(s) — selecting will record this as a split payment.`}
            >
              <ArrowLeftRight className="h-2.5 w-2.5" />
              Split · {doc.existing_match_count ?? 1}
            </Badge>
          )}
          {doc.previously_rejected && (
            <Badge
              variant="outline"
              className="bg-muted text-muted-foreground border-border text-[9px] px-1.5 py-0 gap-0.5 shrink-0"
              title="You rejected this receipt for this transaction before. Automatic matching will not suggest it again — you can still choose it here."
            >
              <Ban className="h-2.5 w-2.5" />
              Previously rejected
            </Badge>
          )}
        </div>
        {/* Row 2: date + amount */}
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <Calendar className="h-3 w-3" />
            {formatDate(doc.date)}
          </span>
          <span className="flex items-center gap-1 font-mono font-medium text-foreground">
            <DollarSign className="h-3 w-3 text-muted-foreground" />
            {formatCurrency(doc.total, doc.currency)}
          </span>
        </div>
        {/* Row 3: filename */}
        <p className="text-[11px] text-muted-foreground/70 truncate">
          {doc.filename}
        </p>
      </div>
      {/* Confidence score — bottom right */}
      <div className="absolute bottom-2 right-2.5">
        <ConfidenceScore score={doc.confidence_score} />
      </div>
    </button>
  );
}

/* ── Main dialog ───────────────────────────────────────────────── */

export function ReceiptSelectorDialog({
  open,
  onOpenChange,
  transaction,
  existingMatchId,
  onMatchSuccess,
  mode = "single",
  onMultiMatchSuccess,
  isExternalPending = false,
}: ReceiptSelectorDialogProps) {
  const queryClient = useQueryClient();
  const searchInputRef = useRef<HTMLInputElement>(null);

  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedDocId, setSelectedDocId] = useState<number | null>(null);
  const [selectedDocIds, setSelectedDocIds] = useState<Set<number>>(new Set());
  const [showUnmatchConfirm, setShowUnmatchConfirm] = useState(false);

  const isMulti = mode === "multi";

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Reset state when transaction changes or dialog opens
  useEffect(() => {
    setSearch("");
    setDebouncedSearch("");
    setSelectedDocId(null);
    setSelectedDocIds(new Set());
    setShowUnmatchConfirm(false);
  }, [transaction?.id, open]);

  // Auto-focus search input
  useEffect(() => {
    if (open) {
      const timer = setTimeout(() => searchInputRef.current?.focus(), 150);
      return () => clearTimeout(timer);
    }
  }, [open]);

  // Available matches query.
  // staleTime: 0 + refetchOnMount: 'always' ensures that a freshly-uploaded
  // receipt is visible immediately when the dialog reopens — without this,
  // newly-uploaded receipts can stay invisible until the cache expires.
  const { data: candidatesData, isLoading } = useQuery<AvailableMatchesResponse>({
    queryKey: ["available-matches", transaction?.id, debouncedSearch],
    queryFn: () =>
      api.get(
        `/api/transactions/${transaction!.id}/available-matches?${new URLSearchParams({
          ...(debouncedSearch ? { search: debouncedSearch } : {}),
          limit: "30",
        }).toString()}`
      ),
    enabled: open && !!transaction?.id,
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });

  // Manual match mutation
  const matchMutation = useMutation({
    mutationFn: ({
      transactionId,
      documentId,
      allowSplit,
    }: {
      transactionId: number;
      documentId: number;
      allowSplit?: boolean;
    }) =>
      api.post("/api/reconciliation/matches/manual", {
        transaction_id: transactionId,
        document_id: documentId,
        allow_split: !!allowSplit,
      }),
    onSuccess: () => {
      toast.success("Receipt matched to this transaction.");
      invalidateAll();
      onOpenChange(false);
      onMatchSuccess();
    },
  });

  // Reassign mutation
  const reassignMutation = useMutation({
    mutationFn: ({ matchId, newDocId }: { matchId: number; newDocId: number }) =>
      api.post(`/api/reconciliation/matches/${matchId}/reassign`, {
        new_document_id: newDocId,
      }),
    onSuccess: () => {
      toast.success("Receipt reassigned.");
      invalidateAll();
      onOpenChange(false);
      onMatchSuccess();
    },
  });

  // Unmatch mutation
  const unmatchMutation = useMutation({
    mutationFn: (matchId: number) =>
      api.post(`/api/reconciliation/matches/${matchId}/unmatch`),
    onSuccess: () => {
      toast.success("Receipt unmatched.");
      invalidateAll();
      onOpenChange(false);
      onMatchSuccess();
    },
  });

  function invalidateAll() {
    queryClient.invalidateQueries({ queryKey: ["transactions"] });
    queryClient.invalidateQueries({ queryKey: ["transaction", transaction?.id] });
    queryClient.invalidateQueries({ queryKey: ["receipts"] });
    queryClient.invalidateQueries({ queryKey: ["review-matches"] });
    queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
    queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    queryClient.invalidateQueries({ queryKey: ["available-matches"] });
    queryClient.invalidateQueries({ queryKey: ["source-files"] });
  }

  const isAnyPending = matchMutation.isPending || reassignMutation.isPending || unmatchMutation.isPending || isExternalPending;
  const isReassign = !!existingMatchId;

  // Separate suggested (top match with highest confidence >= 0.5) from all receipts
  const { suggested, allReceipts } = useMemo(() => {
    if (!candidatesData?.candidates) return { suggested: null as MatchCandidate | null, allReceipts: [] as MatchCandidate[] };

    const sorted = [...candidatesData.candidates].sort(
      (a, b) => b.confidence_score - a.confidence_score
    );

    // Top suggestion: highest confidence if >= 0.5
    const topCandidate = sorted.length > 0 && sorted[0].confidence_score >= 0.5
      ? sorted[0]
      : null;

    // All receipts = everything (including suggested, for visibility in both sections)
    const allReceipts = sorted;

    return { suggested: topCandidate, allReceipts };
  }, [candidatesData]);

  const handleConfirm = () => {
    if (!transaction) return;
    const txnId = typeof transaction.id === "string" ? parseInt(transaction.id, 10) : transaction.id;

    if (isMulti) {
      if (selectedDocIds.size === 0) return;
      onMultiMatchSuccess?.(Array.from(selectedDocIds));
      return;
    }

    if (!selectedDocId) return;
    if (isReassign && existingMatchId) {
      reassignMutation.mutate({ matchId: existingMatchId, newDocId: selectedDocId });
    } else {
      // If the selected receipt is already matched to another transaction,
      // route through the split-payment path (many-to-many).
      const selectedDoc = candidatesData?.candidates.find((c) => c.id === selectedDocId);
      const allowSplit = !!selectedDoc?.is_already_matched;
      matchMutation.mutate({ transactionId: txnId, documentId: selectedDocId, allowSplit });
    }
  };

  const toggleDocId = (id: number) => {
    setSelectedDocIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else if (next.size < 5) {
        next.add(id);
      }
      return next;
    });
  };

  // Client-side coverage calculation for multi mode. Only receipts in the
  // transaction's own currency count toward the ratio — a US$ invoice on a
  // C$ charge is not comparable without FX conversion.
  const multiCoverage = (() => {
    if (!isMulti || !candidatesData) return null;
    const txnCurrency = candidatesData.transaction?.currency || "CAD";
    const selected = candidatesData.candidates.filter((c) =>
      selectedDocIds.has(c.id),
    );
    const sameCurrency = selected.filter(
      (c) => (c.currency || txnCurrency) === txnCurrency,
    );
    const crossCurrencyCount = selected.filter(
      (c) => c.total && (c.currency || txnCurrency) !== txnCurrency,
    ).length;
    const selectedTotal = sameCurrency.reduce(
      (sum, c) => sum + (c.total ? Math.abs(parseFloat(c.total)) : 0),
      0,
    );
    const txnAmt = candidatesData.transaction?.amount
      ? Math.abs(parseFloat(candidatesData.transaction.amount))
      : 0;
    const pct =
      selectedTotal > 0 && txnAmt
        ? selectedTotal / txnAmt
        : crossCurrencyCount > 0
          ? null
          : 0;
    return {
      covered: selectedTotal,
      total: txnAmt,
      pct,
      crossCurrencyCount,
    };
  })();

  const handleUnmatch = () => {
    if (!existingMatchId) return;
    unmatchMutation.mutate(existingMatchId);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-lg font-bold">
            {isReassign ? "Change Match" : "Match Receipt"}
          </DialogTitle>
        </DialogHeader>

        {/* Transaction being matched — prominent header card */}
        {transaction && <TransactionHeader transaction={transaction} />}

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            ref={searchInputRef}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by vendor, filename, amount, or receipt ID…"
            className="pl-9"
          />
        </div>

        {/* Candidate list — scrollable */}
        <div className="flex-1 min-h-0 overflow-y-auto -mx-1 px-1" style={{ maxHeight: "420px" }}>
          {isLoading ? (
            <div className="space-y-2 py-2">
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="flex items-center gap-3 p-3 rounded-lg border border-border/40">
                  <Skeleton className="h-5 w-5 rounded" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-4 w-32 rounded" />
                    <Skeleton className="h-3 w-48 rounded" />
                    <Skeleton className="h-3 w-40 rounded" />
                  </div>
                  <Skeleton className="h-5 w-12 rounded" />
                </div>
              ))}
            </div>
          ) : candidatesData?.candidates.length === 0 ? (
            <div className="py-12 text-center">
              <div className="mx-auto h-12 w-12 rounded-full bg-muted/50 flex items-center justify-center mb-3">
                <FileText className="h-6 w-6 text-muted-foreground/40" />
              </div>
              <p className="text-sm font-medium text-muted-foreground">
                {debouncedSearch
                  ? `No receipts match "${debouncedSearch}".`
                  : "No receipts available."}
              </p>
              <p className="text-xs text-muted-foreground/60 mt-1">
                {debouncedSearch
                  ? "Try a different search or upload the document first."
                  : "Upload receipts first, then match them. Already-matched receipts can also be linked here as split payments."}
              </p>
            </div>
          ) : (
            <div className="space-y-4 py-1" role="listbox" aria-label="Available receipts">
              {/* Suggested match — single best candidate */}
              {suggested && (
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-wider text-primary mb-2 px-1 flex items-center gap-1.5">
                    <Star className="h-3 w-3" />
                    Suggested Match
                  </p>
                  <CandidateRow
                    doc={suggested}
                    selected={isMulti ? selectedDocIds.has(suggested.id) : selectedDocId === suggested.id}
                    onClick={() => isMulti ? toggleDocId(suggested.id) : setSelectedDocId(suggested.id)}
                    isBestMatch
                  />
                </div>
              )}

              {/* All receipts */}
              {allReceipts.length > 0 && (
                <>
                  {suggested && <Separator className="my-1" />}
                  <div>
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-2 px-1">
                      All Receipts ({allReceipts.length})
                    </p>
                    <div className="space-y-1.5">
                      {allReceipts.map((doc) => (
                        <CandidateRow
                          key={doc.id}
                          doc={doc}
                          selected={isMulti ? selectedDocIds.has(doc.id) : selectedDocId === doc.id}
                          onClick={() => isMulti ? toggleDocId(doc.id) : setSelectedDocId(doc.id)}
                        />
                      ))}
                    </div>
                  </div>
                </>
              )}
            </div>
          )}
        </div>

        {/* Multi-mode coverage indicator */}
        {isMulti && multiCoverage && selectedDocIds.size > 0 && (
          <CoverageIndicator
            coveredAmount={multiCoverage.covered}
            totalAmount={multiCoverage.total}
            currency={candidatesData?.transaction?.currency || "CAD"}
            coveragePct={multiCoverage.pct}
            crossCurrencyCount={multiCoverage.crossCurrencyCount}
            className="px-1"
          />
        )}

        {/* Split-payment notice — when the selected receipt is already linked
            to other transactions, say that this will be recorded as a split. */}
        {!isMulti && selectedDocId !== null && (() => {
          const sel = candidatesData?.candidates.find((c) => c.id === selectedDocId);
          if (!sel?.is_already_matched) return null;
          return (
            <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              <div className="font-semibold flex items-center gap-1.5">
                <ArrowLeftRight className="h-3.5 w-3.5" />
                Split-payment link
              </div>
              <div className="mt-0.5">
                Receipt #{sel.id} is already linked to {sel.existing_match_count ?? 1} other
                transaction{(sel.existing_match_count ?? 1) === 1 ? "" : "s"}. Confirming will
                record this transaction as part of a split-payment group covering the same receipt.
              </div>
            </div>
          );
        })()}

        {/* Unmatch confirmation */}
        {showUnmatchConfirm ? (
          <div className="rounded-lg border border-destructive/20 bg-destructive/5 p-3 space-y-2">
            <p className="text-sm text-destructive font-medium">
              Remove this match? The receipt will become available for other transactions.
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowUnmatchConfirm(false)}
                disabled={isAnyPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleUnmatch}
                disabled={isAnyPending}
              >
                {unmatchMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                ) : (
                  <Unlink className="h-3.5 w-3.5 mr-1" />
                )}
                Confirm Unmatch
              </Button>
            </div>
          </div>
        ) : (
          <DialogFooter className="flex gap-2 sm:flex-row">
            {isReassign && existingMatchId && (
              <Button
                variant="outline"
                size="sm"
                className="border-destructive/30 text-destructive hover:bg-destructive/5"
                onClick={() => setShowUnmatchConfirm(true)}
                disabled={isAnyPending}
              >
                <Unlink className="h-3.5 w-3.5 mr-1" />
                Unmatch
              </Button>
            )}
            <div className="flex-1" />
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isAnyPending}
            >
              Cancel
            </Button>
            <Button
              onClick={handleConfirm}
              disabled={isMulti ? selectedDocIds.size === 0 || isAnyPending : !selectedDocId || isAnyPending}
              className="bg-primary hover:bg-primary/90 text-primary-foreground"
            >
              {isAnyPending ? (
                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
              ) : (
                <CheckCircle className="h-4 w-4 mr-1.5" />
              )}
              {isMulti
                ? `Confirm Match${selectedDocIds.size > 0 ? ` (${selectedDocIds.size})` : ""}`
                : isReassign
                  ? "Reassign Match"
                  : "Confirm Match"}
            </Button>
          </DialogFooter>
        )}

        {/* Error display */}
        {(matchMutation.isError || reassignMutation.isError || unmatchMutation.isError) && (
          <p className="text-xs text-destructive mt-1">
            {(matchMutation.error || reassignMutation.error || unmatchMutation.error)?.message ||
              "Failed to save match. Please try again."}
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
