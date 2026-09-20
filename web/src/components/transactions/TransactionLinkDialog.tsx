import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Search, ArrowLeftRight, Star, Wallet, CreditCard, Globe, DollarSign, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { useLinkCandidates, useCreateLink } from "@/hooks/use-transaction-links";
import type { LinkCandidate } from "@/types/transaction-links";

interface TransactionLinkDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  transaction: {
    id: string;
    description: string;
    amount: number;
    currency: string;
    account: string;
    date: string;
    category: string | null;
  } | null;
  onLinkSuccess?: () => void;
}

function AccountIcon({ account }: { account: string }) {
  switch (account) {
    case "CreditCard":
      return <CreditCard className="h-4 w-4" />;
    case "Wise":
      return <Globe className="h-4 w-4" />;
    case "USD":
      return <DollarSign className="h-4 w-4" />;
    default:
      return <Wallet className="h-4 w-4" />;
  }
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr + "T00:00:00");
  return date.toLocaleDateString("en-CA", { year: "numeric", month: "short", day: "numeric" });
}

export function TransactionLinkDialog({
  open,
  onOpenChange,
  transaction,
  onLinkSuccess,
}: TransactionLinkDialogProps) {
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  // Multi-select: one transaction may be linked to several counterparts (a
  // bulk charge refunded item by item). Selections survive re-searching, so
  // the user can find each refund with a different query and link them all
  // in one go — the map preserves the candidate for rows scrolled out of view.
  const [selected, setSelected] = useState<Map<number, LinkCandidate>>(new Map());

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const createLink = useCreateLink();

  // Reset state when dialog opens/closes — including the mutation state, or
  // a stale error banner from a previous failed attempt greets the next
  // transaction's dialog (the component stays mounted across open cycles).
  const resetCreateLink = createLink.reset;
  useEffect(() => {
    if (!open) {
      setSearch("");
      setDebouncedSearch("");
      setSelected(new Map());
      resetCreateLink();
    }
  }, [open, resetCreateLink]);

  const txnId = transaction ? parseInt(transaction.id, 10) : null;
  const { data: candidateData, isLoading } = useLinkCandidates(
    open ? txnId : null,
    debouncedSearch
  );

  const candidates = candidateData?.candidates ?? [];
  const selectable = candidates.filter((c) => !c.unavailable_reason);
  const topCandidate =
    selectable.length > 0 && selectable[0].confidence_score >= 0.5 ? selectable[0] : null;

  const toggle = (candidate: LinkCandidate) => {
    if (candidate.unavailable_reason) return;
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(candidate.id)) next.delete(candidate.id);
      else next.set(candidate.id, candidate);
      return next;
    });
  };

  const selectedList = Array.from(selected.values());
  const anchorCurrency = transaction?.currency ?? "CAD";

  // Running total of what has been picked, against the anchor's own amount —
  // the number that tells the user whether the refund legs they chose actually
  // add up to the charge, shown live before anything is written.
  //
  // Only same-currency legs are summed. A US$500 counterpart against a C$650
  // anchor is not "$500 of $650" at any exchange rate, and printing it that
  // way invents a coverage figure.
  const sameCurrency = selectedList.filter((c) => c.currency === anchorCurrency);
  const crossCurrencyCount = selectedList.length - sameCurrency.length;
  const selectedTotal = sameCurrency.reduce(
    (sum, c) => sum + Math.abs(parseFloat(c.amount) || 0),
    0
  );
  const anchorTotal = transaction ? Math.abs(transaction.amount) : 0;

  // One create call per link type. Collapsing a mixed selection into "OTHER"
  // would silently retype a CREDIT_CARD_PAYMENT or FX_CONVERSION as a
  // non-transfer, and core/transfer_exclusion.py keys income/expense exclusion
  // off exactly those four type names — so both sides of a real transfer would
  // reappear in the totals as income and expense. Each type gets its own group.
  const byLinkType = selectedList.reduce((acc, c) => {
    (acc[c.suggested_link_type] ||= []).push(c.id);
    return acc;
  }, {} as Record<string, number[]>);

  const money = (n: number, currency: string) =>
    n.toLocaleString("en-CA", {
      style: "currency",
      currency: currency === "CAD" || currency === "USD" ? currency : "CAD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });

  const handleConfirm = async () => {
    if (selectedList.length === 0 || !transaction) return;
    const sourceId = parseInt(transaction.id, 10);
    try {
      for (const [linkType, ids] of Object.entries(byLinkType)) {
        await createLink.mutateAsync({
          source_transaction_id: sourceId,
          target_transaction_ids: ids,
          link_type: linkType,
        });
      }
      onOpenChange(false);
      onLinkSuccess?.();
    } catch {
      // The mutation's error state drives the banner below; leave the dialog
      // open with the selection intact so the user can retry or deselect.
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ArrowLeftRight className="h-5 w-5 text-indigo-500" />
            Link to Transactions
          </DialogTitle>
          <p className="text-xs text-muted-foreground">
            Pick one counterpart, or several — a bulk charge refunded item by item
            links to every credit that offsets it.
          </p>
        </DialogHeader>

        {/* Source transaction header */}
        {transaction && (
          <div className="rounded-lg border-2 border-primary/20 bg-primary/[0.02] p-3 space-y-1">
            <div className="flex items-center gap-2 text-sm font-medium">
              <AccountIcon account={transaction.account} />
              <span>{transaction.description}</span>
            </div>
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span>{formatDate(transaction.date)}</span>
              <span className="font-mono font-semibold tabular-nums">
                ${Math.abs(transaction.amount).toLocaleString("en-CA", { minimumFractionDigits: 2 })} {transaction.currency}
              </span>
              <span>{transaction.account}</span>
              {transaction.category && <Badge variant="secondary" className="text-[10px]">{transaction.category}</Badge>}
            </div>
          </div>
        )}

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search every transaction — any field, e.g. text, amount, #ID, regex…"
            className="w-full rounded-md border border-input bg-background pl-9 pr-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
        </div>

        {/* Candidate list */}
        <div className="max-h-[420px] overflow-y-auto space-y-1">
          {isLoading ? (
            <div className="py-8 text-center text-sm text-muted-foreground">Searching...</div>
          ) : candidates.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              {search
                ? "No transactions match your search."
                : "No link suggestions found — search to browse every transaction."}
            </div>
          ) : (
            <>
              {/* Top suggestion */}
              {topCandidate && !search && (
                <div className="mb-2">
                  <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Suggested Link</p>
                  <CandidateRow
                    candidate={topCandidate}
                    isSelected={selected.has(topCandidate.id)}
                    isBest
                    onClick={() => toggle(topCandidate)}
                  />
                </div>
              )}

              {/* All candidates */}
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                All Candidates
              </p>
              {candidates.map((c) => (
                <CandidateRow
                  key={c.id}
                  candidate={c}
                  isSelected={selected.has(c.id)}
                  isBest={false}
                  onClick={() => toggle(c)}
                />
              ))}
            </>
          )}
        </div>

        {/* Running tally — only meaningful once more than one leg is picked,
            or when the single pick doesn't cover the anchor. */}
        {selectedList.length > 0 && (
          <div className="rounded-lg border border-indigo-200/50 bg-indigo-50/40 dark:border-indigo-800/40 dark:bg-indigo-950/20 px-3 py-2 text-xs">
            <div className="flex items-center justify-between">
              <span className="font-medium">
                {selectedList.length} transaction{selectedList.length === 1 ? "" : "s"} selected
              </span>
              {sameCurrency.length > 0 && (
                <span className="font-mono tabular-nums">
                  {money(selectedTotal, anchorCurrency)}
                  {anchorTotal > 0 && (
                    <span className="text-muted-foreground">
                      {" "}of {money(anchorTotal, anchorCurrency)}
                    </span>
                  )}
                </span>
              )}
            </div>
            {anchorTotal > 0 && selectedTotal > anchorTotal + 0.01 && (
              <p className="mt-1 text-amber-700 dark:text-amber-400">
                Selected total exceeds this transaction&apos;s amount — check you haven&apos;t
                picked a transaction that belongs to a different charge.
              </p>
            )}
            {crossCurrencyCount > 0 && (
              <p className="mt-1 text-muted-foreground">
                {crossCurrencyCount} selected in another currency — linked, but not
                included in the total above.
              </p>
            )}
            {Object.keys(byLinkType).length > 1 && (
              <p className="mt-1 text-muted-foreground">
                Mixed link types — saved as {Object.keys(byLinkType).length} separate
                groups so each keeps its own type.
              </p>
            )}
          </div>
        )}

        {/* Actions */}
        {createLink.isError && (
          <p className="text-xs text-destructive" role="alert">
            {createLink.error instanceof Error
              ? createLink.error.message
              : "Failed to create link. Please try again."}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-2 border-t">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleConfirm}
            disabled={selectedList.length === 0 || createLink.isPending}
            className="bg-indigo-600 hover:bg-indigo-700 text-white"
          >
            {createLink.isPending
              ? "Linking..."
              : selectedList.length > 1
                ? `Link ${selectedList.length} Transactions`
                : "Confirm Link"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

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

function ComponentScores({ candidate }: { candidate: LinkCandidate }) {
  const scores = [
    { label: "Amt", value: candidate.amount_score },
    { label: "Date", value: candidate.date_score },
    { label: "Kw", value: candidate.keyword_score },
    { label: "Acct", value: candidate.account_pair_score },
  ];
  return (
    <div className="flex items-center gap-2 mt-1.5">
      {scores.map((s) => (
        <span key={s.label} className="text-[10px] text-muted-foreground">
          <span className="opacity-60">{s.label}</span>{" "}
          <span className="font-mono font-medium">{Math.round(s.value * 100)}%</span>
        </span>
      ))}
    </div>
  );
}

function CandidateRow({
  candidate,
  isSelected,
  isBest,
  onClick,
}: {
  candidate: LinkCandidate;
  isSelected: boolean;
  isBest: boolean;
  onClick: () => void;
}) {
  // A flagged row is shown so the search can be trusted, but it is inert.
  const unavailable = candidate.unavailable_reason;
  const unavailableLabel =
    unavailable === "self"
      ? "This transaction"
      : unavailable === "already_linked"
        ? "Already linked"
        : null;

  return (
    <button
      onClick={onClick}
      role="checkbox"
      aria-checked={isSelected}
      disabled={!!unavailable}
      aria-disabled={!!unavailable}
      title={unavailableLabel ?? undefined}
      className={cn(
        "w-full text-left rounded-lg border p-3 transition-all",
        unavailable
          ? "border-border/60 bg-muted/30 opacity-60 cursor-not-allowed"
          : isSelected
            ? "border-primary bg-primary/5 ring-2 ring-primary/20"
            : "border-border hover:border-primary/30 hover:bg-primary/[0.02]"
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          {unavailableLabel ? (
            <Badge variant="secondary" className="text-[10px] shrink-0">
              {unavailableLabel}
            </Badge>
          ) : (
            <span
              aria-hidden
              className={cn(
                "flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors",
                isSelected
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-muted-foreground/40"
              )}
            >
              {isSelected && <Check className="h-3 w-3" />}
            </span>
          )}
          <AccountIcon account={candidate.account} />
          <span className="text-sm font-medium truncate">{candidate.description}</span>
          <span className="font-mono text-[10px] text-muted-foreground/70 shrink-0">
            #{candidate.id}
          </span>
          {isBest && (
            <Badge className="bg-primary/10 text-primary text-[10px] flex items-center gap-0.5 flex-shrink-0">
              <Star className="h-2.5 w-2.5" /> Best
            </Badge>
          )}
        </div>
        <ConfidenceScore score={candidate.confidence_score} />
      </div>
      <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
        <span>{formatDate(candidate.date)}</span>
        <span className="font-mono font-semibold tabular-nums">
          ${Math.abs(parseFloat(candidate.amount)).toLocaleString("en-CA", { minimumFractionDigits: 2 })} {candidate.currency}
        </span>
        <span>{candidate.account}</span>
        <span className="text-indigo-500 text-[10px]">{candidate.suggested_link_type.replace(/_/g, " ")}</span>
      </div>
      <ComponentScores candidate={candidate} />
    </button>
  );
}
