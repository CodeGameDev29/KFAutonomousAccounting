import { useRef, useCallback } from "react";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { FileQuestion, StickyNote, Link2, FileCheck, ArrowLeftRight, HelpCircle, CircleCheck, CircleSlash, ArrowUp, ArrowDown, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { TransactionLinkBadge } from "./TransactionLinkBadge";
import { CategorySelect } from "./CategorySelect";
import type { CategoryGroup } from "@/types/category-taxonomy";
import type { LinkedTransactionInfo } from "@/types/transaction-links";

export interface ProofInfo {
  doc_id: string;
  filename: string;
  match_id: number;
  group_id: string | null;
  covered_amount: string | null;
}

export interface Transaction {
  id: string;
  date: string;
  description: string;
  amount: number;
  currency: string;
  account: string;
  transaction_type?: "CREDIT" | "DEBIT";
  status: "MATCHED" | "UNMATCHED" | "PENDING_REVIEW" | "LINKED" | "IGNORED" | "EXCLUDED";
  category: string | null;
  matched_doc_id: string | null;
  matched_doc_filename: string | null;
  match_id: number | null;
  confidence: number | null;
  match_confidence?: number | null;
  source_file: string | null;
  note: string | null;
  proofs_count?: number;
  proofs?: ProofInfo[];
  linked_transactions?: LinkedTransactionInfo[];
  // Fields the LLM-based generic bank-statement parser adds.
  institution?: string | null;
  account_type?: string | null;
  import_confidence?: "HIGH" | "MEDIUM" | "LOW" | "LEGACY" | null;
  import_flags?: string[];
  statement_id?: string | null;
  match_explanation?: string | null;
}

export interface SortState {
  column: string | null; // "date" | "amount" | "description" | "category" | "status" | "account"
  direction: "asc" | "desc";
}

interface TransactionTableProps {
  transactions: Transaction[] | undefined;
  isLoading: boolean;
  onRowClick: (transaction: Transaction) => void;
  selectedId: string | null;
  sort?: SortState;
  onSortChange?: (sort: SortState) => void;
  onNoteClick?: (transaction: Transaction) => void;
  onProofClick?: (transaction: Transaction) => void;
  /** Direct click on a proof filename — opens the document itself. */
  onProofFileClick?: (transaction: Transaction, docId: number) => void;
  onLinkTransactionClick?: (transaction: Transaction) => void;
  onCategoryChange?: (transaction: Transaction, nextCategory: string) => void;
  onToggleIgnore?: (transaction: Transaction) => void;
  categories?: string[];
  /** Grouped fixed taxonomy for optgroup rendering. */
  categoryGroups?: CategoryGroup[];
  highlightId?: string | null;
  /** Keyboard-navigated row: ID of the transaction that has keyboard focus */
  focusedId?: string | null;
  /** Multi-select: set of currently selected transaction IDs */
  selectedIds?: Set<number>;
  /** Multi-select: callback when the selection set changes */
  onSelectionChange?: (ids: Set<number>) => void;
  /** Multi-select: callback when a batch action is triggered on selected IDs */
  onBatchAction?: (action: string, ids: number[], extra?: string) => void;
}

function formatCurrency(amount: number, currency: string, type?: string): string {
  const abs = Math.abs(amount);
  // Use transaction type to determine sign when available;
  // fall back to raw amount sign otherwise.
  const isCredit = type ? type === "CREDIT" : amount >= 0;
  const prefix = isCredit ? "+" : "-";
  // Never coerce a non-CAD/USD currency (e.g. EUR from Wise) to a dollar
  // sign — this table has no separate Currency column, so the code must be
  // visible in the amount itself.
  if (currency === "CAD" || currency === "USD") {
    const formatted = abs.toLocaleString("en-CA", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return `${prefix}${formatted}`;
  }
  return `${prefix}${abs.toLocaleString("en-CA", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency}`;
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr + "T00:00:00");
  return date.toLocaleDateString("en-CA", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function truncateFilename(name: string, max: number = 20): string {
  if (name.length <= max) return name;
  const ext = name.lastIndexOf(".");
  if (ext > 0 && name.length - ext <= 5) {
    const available = max - (name.length - ext) - 1;
    return name.substring(0, available) + "\u2026" + name.substring(ext);
  }
  return name.substring(0, max - 1) + "\u2026";
}

function StatusBadge({ status }: { status: Transaction["status"] }) {
  switch (status) {
    case "MATCHED":
      return (
        <Badge className="bg-primary/10 text-primary hover:bg-primary/15 border-primary/20 font-semibold text-xs">
          Matched
        </Badge>
      );
    case "UNMATCHED":
      return (
        <Badge
          variant="secondary"
          className="bg-muted text-muted-foreground border-border font-medium text-xs"
        >
          Unmatched
        </Badge>
      );
    case "PENDING_REVIEW":
      return (
        <Badge className="bg-amber-100 text-amber-800 hover:bg-amber-100 border-amber-200 font-semibold text-xs dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-800">
          Review
        </Badge>
      );
    case "LINKED":
      return (
        <Badge className="bg-indigo-100 text-indigo-800 hover:bg-indigo-100 border-indigo-200 font-semibold text-xs dark:bg-indigo-950/40 dark:text-indigo-200 dark:border-indigo-800">
          Linked
        </Badge>
      );
    // "No receipt" is a resolved, done state — the row needs no proof and no
    // note, and its money still counts. Styled as muted-success, never greyed
    // out: a dead-looking row reads as "this doesn't count", which is exactly
    // the wrong reading.
    case "IGNORED":
      return (
        <Badge className="bg-emerald-100/70 text-emerald-800 hover:bg-emerald-100/70 border-emerald-200 font-medium text-xs dark:bg-emerald-950/30 dark:text-emerald-200/90 dark:border-emerald-900">
          No receipt
        </Badge>
      );
    // The genuinely-doesn't-count state: the row isn't real.
    case "EXCLUDED":
      return (
        <Badge variant="secondary" className="bg-muted/60 text-muted-foreground/70 border-border/50 font-medium text-xs line-through">
          Excluded
        </Badge>
      );
    default:
      return null;
  }
}

const COLUMN_HELP: Record<string, string> = {
  ID: "Internal transaction identifier assigned when the transaction is imported.",
  Date: "The date the transaction posted to your account, as reported by the bank statement.",
  Description: "The raw description from the bank statement — usually the merchant name or transfer memo.",
  Category: "Government-standard category (Canada ISED Financial Performance Data + CRA GIFI). Pick from the fixed list — categories can't be renamed or created.",
  Amount: "The transaction amount. + means money in (credit), − means money out (debit), based on the bank's transaction type.",
  Status: "Reconciliation state: Matched (linked to a receipt), Unmatched, Review (needs attention), Linked (cross-statement), No receipt (self-evident — needs no proof, but the amount still counts), or Excluded (not a real transaction — the amount is left out of all totals).",
  Confidence: "Match confidence score (0–100%) for the linked receipt or cross-statement link. Empty for unmatched rows and rows that need no receipt.",
  Proof: "The receipt or invoice matched to this transaction — acts as audit proof that the expense is legitimate.",
  Notes: "Optional freeform note you can add to remember context about this transaction.",
};

function ColumnHeaderLabel({
  label,
  align = "left",
  sortKey,
  sort,
  onSortChange,
}: {
  label: string;
  align?: "left" | "right";
  sortKey?: string;
  sort?: SortState;
  onSortChange?: (sort: SortState) => void;
}) {
  const isSorted = !!sortKey && sort?.column === sortKey;
  const canSort = !!sortKey && !!onSortChange;

  const handleClick = () => {
    if (!canSort) return;
    if (isSorted) {
      onSortChange({ column: sortKey, direction: sort.direction === "asc" ? "desc" : "asc" });
    } else {
      onSortChange({ column: sortKey, direction: "asc" });
    }
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1",
        align === "right" && "justify-end",
        canSort && "cursor-pointer select-none hover:text-foreground transition-colors"
      )}
      onClick={canSort ? handleClick : undefined}
    >
      {label}
      {isSorted && (
        sort.direction === "asc"
          ? <ArrowUp className="h-3 w-3 text-primary flex-shrink-0" strokeWidth={2.5} />
          : <ArrowDown className="h-3 w-3 text-primary flex-shrink-0" strokeWidth={2.5} />
      )}
      <span
        title={COLUMN_HELP[label]}
        aria-label={`${label} column help`}
        className="inline-flex cursor-help"
      >
        <HelpCircle
          className="h-3 w-3 text-muted-foreground/50 hover:text-muted-foreground flex-shrink-0"
          strokeWidth={2.5}
        />
      </span>
    </span>
  );
}

function TableSkeleton() {
  return (
    <div className="space-y-0 p-1">
      {Array.from({ length: 8 }).map((_, i) => (
        <div
          key={i}
          className="flex items-center gap-4 px-4 py-3.5 border-b border-border/40 last:border-0"
        >
          <Skeleton className="h-4 w-10 rounded-md" />
          <Skeleton className="h-4 w-24 rounded-md" />
          <Skeleton className="h-4 w-48 flex-1 rounded-md" />
          <Skeleton className="h-4 w-24 rounded-md" />
          <Skeleton className="h-6 w-20 rounded-full" />
          <Skeleton className="h-4 w-20 rounded-md" />
          <Skeleton className="h-4 w-4 rounded" />
          <Skeleton className="h-4 w-16 rounded-md" />
        </div>
      ))}
    </div>
  );
}

export function TransactionTable({
  transactions,
  isLoading,
  onRowClick,
  selectedId,
  sort,
  onSortChange,
  onNoteClick,
  onProofClick,
  onProofFileClick,
  onLinkTransactionClick,
  onCategoryChange,
  onToggleIgnore,
  categoryGroups,
  highlightId,
  focusedId,
  selectedIds,
  onSelectionChange,
  onBatchAction,
}: TransactionTableProps) {
  // --- Multi-select state ---
  const multiSelectEnabled = Boolean(selectedIds && onSelectionChange);
  const lastClickedIndexRef = useRef<number | null>(null);

  const handleCheckboxChange = useCallback(
    (txnId: number, rowIndex: number, shiftKey: boolean) => {
      if (!selectedIds || !onSelectionChange || !transactions) return;

      const next = new Set(selectedIds);

      if (shiftKey && lastClickedIndexRef.current !== null) {
        // Range selection: select all between last click and current
        const start = Math.min(lastClickedIndexRef.current, rowIndex);
        const end = Math.max(lastClickedIndexRef.current, rowIndex);
        for (let i = start; i <= end; i++) {
          next.add(Number(transactions[i].id));
        }
      } else {
        // Toggle single item
        if (next.has(txnId)) {
          next.delete(txnId);
        } else {
          next.add(txnId);
        }
      }

      lastClickedIndexRef.current = rowIndex;
      onSelectionChange(next);
    },
    [selectedIds, onSelectionChange, transactions]
  );

  const handleSelectAll = useCallback(() => {
    if (!selectedIds || !onSelectionChange || !transactions) return;
    const allIds = transactions.map((t) => Number(t.id));
    const allSelected = allIds.length > 0 && allIds.every((id) => selectedIds.has(id));
    if (allSelected) {
      // Deselect all on current page
      const next = new Set(selectedIds);
      for (const id of allIds) next.delete(id);
      onSelectionChange(next);
    } else {
      // Select all on current page
      const next = new Set(selectedIds);
      for (const id of allIds) next.add(id);
      onSelectionChange(next);
    }
  }, [selectedIds, onSelectionChange, transactions]);

  if (isLoading) {
    return <TableSkeleton />;
  }

  if (!transactions || transactions.length === 0) {
    return (
      <div className="flex h-[400px] items-center justify-center text-muted-foreground">
        <div className="text-center space-y-3">
          <div className="mx-auto h-16 w-16 rounded-full bg-primary/5 flex items-center justify-center">
            <FileQuestion className="h-8 w-8 text-primary/30" />
          </div>
          <p className="text-sm font-medium">No transactions found.</p>
          <p className="text-xs text-muted-foreground/70 max-w-[260px]">
            Upload receipts or bank statements to get started, or adjust your
            filters.
          </p>
        </div>
      </div>
    );
  }

  // Compute select-all checkbox state for current page
  const pageIds = transactions?.map((t) => Number(t.id)) ?? [];
  const selectedCount = selectedIds ? pageIds.filter((id) => selectedIds.has(id)).length : 0;
  const allPageSelected = pageIds.length > 0 && selectedCount === pageIds.length;
  const somePageSelected = selectedCount > 0 && selectedCount < pageIds.length;
  const totalSelected = selectedIds?.size ?? 0;

  return (
    <div className="overflow-x-auto relative">
      {/* Floating batch action toolbar */}
      {multiSelectEnabled && totalSelected > 0 && (
        <div className="sticky top-0 z-20 flex items-center gap-3 bg-primary/[0.06] border border-primary/20 rounded-lg px-4 py-2 mb-2 shadow-sm backdrop-blur-sm">
          <span className="text-sm font-semibold text-foreground tabular-nums">
            {totalSelected} selected
          </span>
          <div className="h-4 w-px bg-border" />
          {onBatchAction && (
            <>
              {categoryGroups && categoryGroups.length > 0 && (
                <CategorySelect
                  value={null}
                  groups={categoryGroups}
                  placeholder="Categorize as…"
                  onChange={(category) =>
                    onBatchAction("categorize", Array.from(selectedIds!), category)
                  }
                  stopPropagation
                  ariaLabel="Categorize selected transactions"
                  className="rounded-md border border-primary/20 bg-primary/5 px-2 py-1.5 text-xs font-medium text-primary focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
              )}
              <button
                type="button"
                onClick={() => onBatchAction("ignore", Array.from(selectedIds!))}
                title="Mark as self-evident — no receipt or note needed. The amounts still count in your totals."
                className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium bg-muted text-muted-foreground hover:bg-muted/80 transition-colors"
              >
                <CircleCheck className="h-3.5 w-3.5" />
                No receipt needed
              </button>
            </>
          )}
          <button
            type="button"
            onClick={() => onSelectionChange?.(new Set())}
            className="ml-auto inline-flex items-center gap-1 rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
          >
            <X className="h-3 w-3" />
            Clear
          </button>
        </div>
      )}
      <table className="w-full">
        <thead>
          <tr className="border-b-2 border-primary/10 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground bg-primary/[0.02]">
            {multiSelectEnabled && (
              <th className="px-3 py-3.5 w-[40px]">
                <input
                  type="checkbox"
                  checked={allPageSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = somePageSelected;
                  }}
                  onChange={handleSelectAll}
                  onClick={(e) => e.stopPropagation()}
                  className="h-4 w-4 rounded border-border text-primary focus:ring-primary/30 cursor-pointer accent-[hsl(var(--primary))]"
                  aria-label="Select all transactions on this page"
                />
              </th>
            )}
            <th className="px-3 py-3.5 w-[64px]"><ColumnHeaderLabel label="TXN ID" /></th>
            <th className="px-4 py-3.5"><ColumnHeaderLabel label="Date" sortKey="date" sort={sort} onSortChange={onSortChange} /></th>
            <th className="px-4 py-3.5"><ColumnHeaderLabel label="Description" sortKey="description" sort={sort} onSortChange={onSortChange} /></th>
            <th className="px-4 py-3.5"><ColumnHeaderLabel label="Category" sortKey="category" sort={sort} onSortChange={onSortChange} /></th>
            <th className="px-4 py-3.5 text-right"><ColumnHeaderLabel label="Amount" align="right" sortKey="amount" sort={sort} onSortChange={onSortChange} /></th>
            <th className="px-4 py-3.5"><ColumnHeaderLabel label="Status" sortKey="status" sort={sort} onSortChange={onSortChange} /></th>
            <th className="px-4 py-3.5"><ColumnHeaderLabel label="Confidence" /></th>
            <th className="hidden lg:table-cell px-4 py-3.5"><ColumnHeaderLabel label="Proof" /></th>
            <th className="hidden lg:table-cell px-4 py-3.5"><ColumnHeaderLabel label="Notes" /></th>
          </tr>
        </thead>
        <tbody>
          {transactions.map((txn, index) => (
            <tr
              key={txn.id}
              data-txn-id={txn.id}
              onClick={() => onRowClick(txn)}
              className={cn(
                "border-b border-border/40 cursor-pointer transition-all duration-150",
                "hover:bg-primary/[0.03] hover:shadow-[inset_3px_0_0_hsl(var(--primary))]",
                index % 2 === 1 && "bg-muted/20",
                txn.status === "PENDING_REVIEW" &&
                  "bg-amber-50/50 dark:bg-amber-950/10",
                selectedId != null && String(selectedId) === String(txn.id) &&
                  "bg-primary/[0.06] shadow-[inset_3px_0_0_hsl(var(--primary))]",
                focusedId != null && String(focusedId) === String(txn.id) &&
                  "ring-2 ring-primary/40 ring-inset bg-primary/[0.04]",
                highlightId != null && String(highlightId) === String(txn.id) && "row-linked-highlight"
              )}
            >
              {multiSelectEnabled && (
                <td className="px-3 py-3">
                  <input
                    type="checkbox"
                    checked={selectedIds!.has(Number(txn.id))}
                    onChange={() => {/* handled by onClick */}}
                    onClick={(e) => {
                      e.stopPropagation();
                      handleCheckboxChange(Number(txn.id), index, e.shiftKey);
                    }}
                    className="h-4 w-4 rounded border-border text-primary focus:ring-primary/30 cursor-pointer accent-[hsl(var(--primary))]"
                    aria-label={`Select transaction ${txn.id}`}
                  />
                </td>
              )}
              <td className="px-3 py-3 text-[11px] font-mono text-muted-foreground/70 tabular-nums">
                #{txn.id}
              </td>
              <td className="px-4 py-3 text-sm whitespace-nowrap text-foreground/80">
                {formatDate(txn.date)}
              </td>
              <td className="px-4 py-3 text-sm max-w-[300px] truncate font-medium">
                {txn.description}
              </td>
              <td className="px-4 py-3 text-sm min-w-[240px] max-w-[320px]">
                {onCategoryChange && categoryGroups && categoryGroups.length > 0 ? (
                  <CategorySelect
                    value={txn.category}
                    groups={categoryGroups}
                    onChange={(next) => onCategoryChange(txn, next)}
                    stopPropagation
                    ariaLabel={`Category for transaction ${txn.id}`}
                    className={cn(
                      "min-w-[220px] flex-1 max-w-[280px] rounded-md border border-transparent bg-transparent px-1.5 py-0.5 text-xs",
                      "hover:border-border/70 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/30",
                      txn.category ? "text-muted-foreground" : "text-muted-foreground/40 italic",
                    )}
                  />
                ) : (
                  <span className="text-muted-foreground truncate block">
                    {txn.category ?? (
                      <span className="text-muted-foreground/40">--</span>
                    )}
                  </span>
                )}
              </td>
              <td
                className={cn(
                  "px-4 py-3 text-sm text-right whitespace-nowrap font-mono font-semibold tabular-nums",
                  txn.transaction_type === "CREDIT" ? "text-primary" : "text-foreground"
                )}
              >
                {formatCurrency(txn.amount, txn.currency, txn.transaction_type)}
              </td>
              <td className="px-4 py-3">
                <div className="flex items-center gap-1.5">
                  <StatusBadge status={txn.status} />
                  {onToggleIgnore && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onToggleIgnore(txn);
                      }}
                      className={cn(
                        "inline-flex h-6 w-6 items-center justify-center rounded-md transition-colors",
                        "text-muted-foreground/60 hover:text-foreground hover:bg-muted/60",
                      )}
                      title={
                        txn.status === "IGNORED"
                          ? "This needs a receipt after all"
                          : "No receipt needed — self-evident. The amount still counts in your totals."
                      }
                      aria-label={
                        txn.status === "IGNORED"
                          ? "Mark transaction as needing a receipt"
                          : "Mark transaction as needing no receipt"
                      }
                    >
                      {txn.status === "IGNORED" ? (
                        <CircleSlash className="h-3.5 w-3.5" />
                      ) : (
                        <CircleCheck className="h-3.5 w-3.5" />
                      )}
                    </button>
                  )}
                </div>
              </td>
              {/* Match Confidence column */}
              <td className="px-4 py-3">
                {(() => {
                  // No match is expected for these, so no score.
                  if (txn.status === "IGNORED" || txn.status === "UNMATCHED" || txn.status === "EXCLUDED") {
                    return <span className="text-muted-foreground/30 text-xs">—</span>;
                  }
                  const score = txn.match_confidence;
                  if (score == null) {
                    return <span className="text-muted-foreground/30 text-xs">—</span>;
                  }
                  const pct = Math.round(score * 100);
                  const tier =
                    pct >= 90 ? "high" : pct >= 70 ? "med" : "low";
                  const badge = (
                    <Badge
                      className={cn(
                        "font-semibold text-[10px] border px-1.5 py-0 font-mono tabular-nums cursor-default",
                        tier === "high" &&
                          "bg-primary/10 text-primary border-primary/20",
                        tier === "med" &&
                          "bg-amber-100 text-amber-800 border-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-800",
                        tier === "low" &&
                          "bg-destructive/10 text-destructive border-destructive/20"
                      )}
                    >
                      {pct}%
                    </Badge>
                  );
                  if (txn.match_explanation) {
                    return (
                      <TooltipProvider delayDuration={200}>
                        <Tooltip>
                          <TooltipTrigger asChild>{badge}</TooltipTrigger>
                          <TooltipContent side="left" className="max-w-xs text-xs leading-relaxed">
                            {txn.match_explanation}
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    );
                  }
                  return badge;
                })()}
              </td>
              {/* Proof column — no proof is expected for rows that need no
                  receipt, or for rows that aren't real.
                  Whitespace click opens the change-match dialog; the
                  filename buttons inside stopPropagation and open the
                  document itself. */}
              <td
                className="hidden lg:table-cell px-4 py-3"
                onClick={(e) => {
                  if (txn.status === "IGNORED" || txn.status === "EXCLUDED") return;
                  e.stopPropagation();
                  onProofClick?.(txn);
                }}
              >
                {/* "Needs no receipt" is not the same as "has nothing to
                    show". A row can be IGNORED (a card payment, a transfer)
                    AND be one leg of a cross-statement link — that link is the
                    proof, and hiding it behind "Not needed" would leave a card
                    payment looking like an unexplained ignored row. Keep "Not
                    needed" only when there is genuinely nothing to show. */}
                {(txn.status === "IGNORED" || txn.status === "EXCLUDED") &&
                !(txn.linked_transactions && txn.linked_transactions.length > 0) ? (
                  <span className="text-xs text-muted-foreground/50">Not needed</span>
                ) : (
                <div className="flex flex-col gap-1">
                  {/* Document proofs — show every proof vertically */}
                  {(txn.status === "MATCHED" || txn.status === "PENDING_REVIEW") && txn.proofs && txn.proofs.length > 0 ? (
                    txn.proofs.map((proof, idx) => {
                      const isReview = txn.status === "PENDING_REVIEW";
                      return (
                        <button
                          key={proof.match_id ?? idx}
                          onClick={(e) => {
                            e.stopPropagation();
                            if (onProofFileClick && proof.doc_id) {
                              onProofFileClick(txn, Number(proof.doc_id));
                            } else {
                              onProofClick?.(txn);
                            }
                          }}
                          className={cn(
                            "text-sm hover:underline truncate max-w-[180px] flex items-center gap-1",
                            isReview
                              ? "text-amber-700 dark:text-amber-300"
                              : "text-primary"
                          )}
                          title={`View ${proof.filename}`}
                          aria-label={`View proof document ${proof.filename}`}
                        >
                          <FileCheck className={cn(
                            "h-3.5 w-3.5 flex-shrink-0",
                            isReview ? "text-amber-500" : "text-primary/60"
                          )} />
                          <span className="truncate">
                            {truncateFilename(proof.filename, 18)}
                          </span>
                        </button>
                      );
                    })
                  ) : (txn.status === "MATCHED" || txn.status === "PENDING_REVIEW") && txn.matched_doc_filename ? (
                    /* Fallback for transactions without proofs array */
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (onProofFileClick && txn.matched_doc_id) {
                          onProofFileClick(txn, Number(txn.matched_doc_id));
                        } else {
                          onProofClick?.(txn);
                        }
                      }}
                      className={cn(
                        "text-sm hover:underline truncate max-w-[180px] flex items-center gap-1",
                        txn.status === "PENDING_REVIEW"
                          ? "text-amber-700 dark:text-amber-300"
                          : "text-primary"
                      )}
                      title={`View ${txn.matched_doc_filename}`}
                      aria-label={`View proof document ${txn.matched_doc_filename}`}
                    >
                      <FileCheck className={cn(
                        "h-3.5 w-3.5 flex-shrink-0",
                        txn.status === "PENDING_REVIEW" ? "text-amber-500" : "text-primary/60"
                      )} />
                      <span className="truncate">
                        {truncateFilename(txn.matched_doc_filename, 18)}
                      </span>
                    </button>
                  ) : null}

                  {/* Transaction link proof. A transaction can be linked to
                      several counterparts (a bulk charge and each credit that
                      refunds part of it) — show the first and count the rest
                      rather than silently hiding them. */}
                  {txn.linked_transactions && txn.linked_transactions.length > 0 ? (
                    <div className="flex items-center gap-1.5">
                      <TransactionLinkBadge link={txn.linked_transactions[0]} />
                      {txn.linked_transactions.length > 1 && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            onLinkTransactionClick?.(txn);
                          }}
                          className="text-xs font-medium text-indigo-600 dark:text-indigo-400 hover:underline shrink-0"
                          title={`Linked to ${txn.linked_transactions.length} transactions`}
                          aria-label={`Linked to ${txn.linked_transactions.length} transactions`}
                        >
                          +{txn.linked_transactions.length - 1}
                        </button>
                      )}
                      {/* Adding another leg must stay reachable once the first
                          link exists — otherwise a split refund can never be
                          completed from the grid. */}
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onLinkTransactionClick?.(txn);
                        }}
                        className="text-indigo-500 hover:text-indigo-700 transition-colors shrink-0"
                        aria-label={`Link another transaction to ${txn.description}`}
                        title="Link another transaction"
                      >
                        <ArrowLeftRight className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : null}

                  {/* Unmatched actions */}
                  {txn.status === "UNMATCHED" && !txn.matched_doc_filename && (!txn.linked_transactions || txn.linked_transactions.length === 0) && (
                    <div className="flex items-center gap-2">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onProofClick?.(txn);
                        }}
                        className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
                        aria-label={`Link receipt to ${txn.description}`}
                      >
                        <Link2 className="h-3.5 w-3.5" />
                        <span>Link</span>
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onLinkTransactionClick?.(txn);
                        }}
                        className="text-sm text-indigo-500 hover:text-indigo-700 flex items-center gap-1 transition-colors"
                        aria-label={`Link to transaction for ${txn.description}`}
                      >
                        <ArrowLeftRight className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  )}

                </div>
                )}
              </td>
              <td className="hidden lg:table-cell px-4 py-3">
                <button
                  className={cn(
                    "flex items-center gap-1.5 max-w-[160px] rounded-md px-2 py-1 text-xs transition-colors",
                    txn.note
                      ? "text-primary/80 bg-primary/5 hover:bg-primary/10"
                      : "text-muted-foreground/40 hover:text-muted-foreground hover:bg-muted/50"
                  )}
                  title={txn.note ? txn.note : "Add a note"}
                  onClick={(e) => {
                    e.stopPropagation();
                    onNoteClick?.(txn);
                  }}
                >
                  <StickyNote className="h-3.5 w-3.5 flex-shrink-0" />
                  {txn.note ? (
                    <span className="truncate">{txn.note}</span>
                  ) : (
                    <span className="italic">Add note</span>
                  )}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
