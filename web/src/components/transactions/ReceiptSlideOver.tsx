import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetFooter,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CheckCircle,
  XCircle,
  FileText,
  Loader2,
  ShieldCheck,
  Clock,
  HelpCircle,
  Link2,
  Unlink,
  StickyNote,
  Paperclip,
  Save,
  X as XIcon,
  Download,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ArrowLeftRight, Trash2 } from "lucide-react";
import { TransactionChain } from "./TransactionChain";
import { ProofBundle } from "./ProofBundle";
import { ReceiptViewerModal } from "./ReceiptViewerModal";
import { CategorySelect } from "./CategorySelect";
import type { CategoryGroup } from "@/types/category-taxonomy";
import { MatchExplanation } from "@/components/MatchExplanation";
import { useDeleteLink, useTransactionLinks } from "@/hooks/use-transaction-links";
import {
  useTransactionProofs,
  useRemoveProof,
} from "@/hooks/use-transaction-proofs";
import type { Transaction } from "./TransactionTable";
import { API_URL } from "@/lib/constants";
import { useAuthStore } from "@/stores/auth-store";

interface TransactionDetail extends Transaction {
  bank_reference: string | null;
  match_score: number | null;
  matched_document: {
    id: string;
    vendor: string;
    date: string;
    total: number;
    currency: string;
    file_name: string;
    file_url: string | null;
    doc_type: string;
    confidence: number;
  } | null;
  match_explanation: string | null;
  match_sub_scores: {
    amount_score: number | null;
    date_score: number | null;
    vendor_score: number | null;
  } | null;
}

interface ReviewResponse {
  status: string;
  transaction_id: string;
}

interface ReceiptSlideOverProps {
  transaction: Transaction | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMatchManually?: (txn: Transaction) => void;
  onUnmatch?: (txn: Transaction) => void;
  onRematch?: (txn: Transaction) => void;
  onLinkTransaction?: (txn: Transaction) => void;
  onChainNavigate?: (txnId: number, sourceFile: string) => void;
  onAttachAnother?: (txn: Transaction) => void;
  onCategoryChange?: (txn: Transaction, nextCategory: string) => void;
  categories?: string[];
  categoryGroups?: CategoryGroup[];
}

function formatCurrency(amount: number, currency: string): string {
  return amount.toLocaleString("en-CA", {
    style: "currency",
    currency: currency === "CAD" || currency === "USD" ? currency : "CAD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function ConfidenceBadge({ confidence }: { confidence: number }) {
  const percent = Math.round(confidence * 100);
  let colorClass: string;
  let Icon: typeof ShieldCheck;
  if (percent >= 80) {
    colorClass =
      "bg-primary/10 text-primary border-primary/20";
    Icon = ShieldCheck;
  } else if (percent >= 60) {
    colorClass =
      "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800";
    Icon = Clock;
  } else {
    colorClass =
      "bg-destructive/10 text-destructive border-destructive/20";
    Icon = HelpCircle;
  }

  return (
    <Badge className={cn("font-mono font-semibold text-xs gap-1", colorClass)}>
      <Icon className="h-3 w-3" />
      {percent}%
    </Badge>
  );
}

function DetailRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex justify-between items-start py-2 border-b border-border/30 last:border-0">
      <span className="text-xs font-medium uppercase tracking-wider text-primary/60">
        {label}
      </span>
      <span
        className={cn(
          "text-sm font-medium text-right max-w-[60%]",
          mono && "font-mono"
        )}
      >
        {value}
      </span>
    </div>
  );
}

interface NoteAttachment {
  id: number;
  filename: string;
  mime_type: string | null;
  file_size: number | null;
  created_at: string | null;
}

function NotesSection({
  transactionId,
  initialNote,
}: {
  transactionId: number;
  initialNote: string | null;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string>(initialNote ?? "");
  const [savedNote, setSavedNote] = useState<string>(initialNote ?? "");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Auth-aware file view: fetches with bearer, opens blob in new tab.
  // Uses the same pattern as Transactions.viewFile to keep popup blockers
  // happy (must call window.open synchronously inside the user gesture).
  const viewAttachment = async (attachmentId: number) => {
    const newWin = window.open("about:blank", "_blank", "noopener,noreferrer");
    try {
      const { getAccessToken } = useAuthStore.getState();
      const token = await getAccessToken();
      const res = await fetch(
        `${API_URL}/api/transactions/${transactionId}/note-attachments/${attachmentId}/download`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {}, redirect: "follow" },
      );
      if (!res.ok) {
        if (newWin) newWin.close();
        return;
      }
      const blob = await res.blob();
      const blobUrl = URL.createObjectURL(blob);
      if (newWin) newWin.location.href = blobUrl;
    } catch {
      if (newWin) newWin.close();
    }
  };

  useEffect(() => {
    setDraft(initialNote ?? "");
    setSavedNote(initialNote ?? "");
  }, [transactionId, initialNote]);

  const saveNote = useMutation({
    mutationFn: (text: string) =>
      api.patch(`/api/transactions/${transactionId}/note`, {
        note: text.trim() ? text : null,
      }),
    onSuccess: (_data, text) => {
      setSavedNote(text);
      toast.success(text.trim() ? "Note saved." : "Note cleared.");
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["transaction", String(transactionId)] });
    },
    onError: () => {
      toast.error("Couldn't save note — please try again.");
    },
  });

  const { data: attachmentsData } = useQuery<{ attachments: NoteAttachment[] }>({
    queryKey: ["note-attachments", transactionId],
    queryFn: () => api.get(`/api/transactions/${transactionId}/note-attachments`),
    enabled: transactionId > 0,
  });
  const attachments = attachmentsData?.attachments ?? [];

  const uploadAttachment = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload(
        `/api/transactions/${transactionId}/note-attachments`,
        form,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["note-attachments", transactionId] });
    },
  });

  const deleteAttachment = useMutation({
    mutationFn: (attachmentId: number) =>
      api.delete(`/api/transactions/${transactionId}/note-attachments/${attachmentId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["note-attachments", transactionId] });
    },
  });

  const dirty = draft !== savedNote;

  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3 flex items-center gap-1.5">
        <StickyNote className="h-3.5 w-3.5" />
        Notes & Supplementary Docs
      </h4>
      <div className="rounded-xl border border-border/60 bg-card p-4 shadow-warm-sm space-y-3">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Add a comment about this transaction (e.g. what it's for, attach a doc to back it up)…"
          rows={4}
          className={cn(
            "w-full rounded-lg border border-border/60 bg-background p-3 text-sm leading-relaxed",
            "placeholder:text-muted-foreground/40",
            "focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary/40 transition-colors",
          )}
        />
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            onClick={() => saveNote.mutate(draft)}
            disabled={!dirty || saveNote.isPending}
            className="bg-primary hover:bg-primary/90 text-primary-foreground rounded-lg shadow-warm-sm"
          >
            {saveNote.isPending ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <Save className="h-3.5 w-3.5 mr-1.5" />
            )}
            {saveNote.isPending ? "Saving…" : dirty ? "Save note" : "Saved"}
          </Button>
          {dirty && (
            <button
              type="button"
              onClick={() => setDraft(savedNote)}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              Revert
            </button>
          )}
          {saveNote.isError && (
            <span className="text-xs text-destructive">Failed — try again</span>
          )}
        </div>

        {/* Attachments */}
        <div className="pt-2 border-t border-border/40 space-y-2">
          <div className="flex items-center justify-between">
            <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Supplementary documents
              {attachments.length > 0 ? ` (${attachments.length})` : ""}
            </p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadAttachment.isPending}
              className="h-7 px-2 text-xs rounded-md border-primary/30 text-primary hover:bg-primary/5"
            >
              {uploadAttachment.isPending ? (
                <Loader2 className="h-3 w-3 mr-1 animate-spin" />
              ) : (
                <Paperclip className="h-3 w-3 mr-1" />
              )}
              Upload
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) uploadAttachment.mutate(f);
                if (fileInputRef.current) fileInputRef.current.value = "";
              }}
            />
          </div>
          {attachments.length === 0 ? (
            <p className="text-xs text-muted-foreground/60 italic">
              No supplementary documents attached.
            </p>
          ) : (
            <ul className="space-y-1">
              {attachments.map((att) => (
                <li
                  key={att.id}
                  className="flex items-center gap-2 rounded-md bg-muted/30 px-2.5 py-1.5"
                >
                  <FileText className="h-3.5 w-3.5 text-primary/60 flex-shrink-0" />
                  <button
                    type="button"
                    onClick={() => viewAttachment(att.id)}
                    className="flex-1 min-w-0 text-left text-xs text-primary hover:underline truncate"
                    title={att.filename}
                  >
                    {att.filename}
                  </button>
                  <button
                    type="button"
                    onClick={() => viewAttachment(att.id)}
                    className="text-muted-foreground/60 hover:text-foreground"
                    title="Open in new tab"
                  >
                    <Download className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    onClick={() => deleteAttachment.mutate(att.id)}
                    disabled={deleteAttachment.isPending}
                    className="text-muted-foreground/60 hover:text-destructive disabled:opacity-50"
                    title="Remove attachment"
                  >
                    <XIcon className="h-3 w-3" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

export function ReceiptSlideOver({
  transaction,
  open,
  onOpenChange,
  onMatchManually,
  onUnmatch,
  // `onRematch` is part of the prop contract and Transactions.tsx passes one,
  // but nothing in this panel calls it, so it is not destructured.
  onLinkTransaction,
  onChainNavigate,
  onAttachAnother,
  onCategoryChange,
  categoryGroups,
}: ReceiptSlideOverProps) {
  const queryClient = useQueryClient();

  const { data: detail, isLoading } = useQuery<TransactionDetail>({
    queryKey: ["transaction", transaction?.id],
    queryFn: () => api.get(`/api/transactions/${transaction!.id}`),
    enabled: !!transaction?.id && open,
  });

  const reviewMutation = useMutation<
    ReviewResponse,
    Error,
    { action: "approve" | "reject" }
  >({
    mutationFn: ({ action }) =>
      api.post(`/api/transactions/${transaction!.id}/review`, { action }),
    onSuccess: (_data, { action }) => {
      toast.success(action === "approve" ? "Match approved." : "Match rejected.");
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({
        queryKey: ["transaction", transaction?.id],
      });
      queryClient.invalidateQueries({ queryKey: ["reviews-pending"] });
      onOpenChange(false);
    },
    onError: () => {
      toast.error("Couldn't record that decision — please try again.");
    },
  });

  const deleteLinkMutation = useDeleteLink();
  // Which single link row is mid-removal. A shared boolean is wrong twice over
  // once a transaction can hold several links: it greys the whole section, and
  // it clears only when the link array empties, so removing one leg of a group
  // would leave the remaining legs disabled.
  const [unlinkingId, setUnlinkingId] = useState<number | null>(null);
  const [confirmUnmatch, setConfirmUnmatch] = useState(false);
  const [isUnmatching, setIsUnmatching] = useState(false);
  const [viewerDocId, setViewerDocId] = useState<number | null>(null);

  // Proofs for this transaction — used by the bulk Unmatch button so it can
  // remove every attached proof in one click, through the same mutation the
  // per-proof trashcan icons in ProofBundle use.
  const txnIdNum = transaction ? parseInt(transaction.id, 10) : null;
  const { data: proofsData } = useTransactionProofs(txnIdNum);
  const removeProof = useRemoveProof();

  // Every link on this transaction, with the offset arithmetic for the group.
  const { data: linksData } = useTransactionLinks(txnIdNum);

  // Reset unlinking and unmatch confirmation state when switching transactions
  useEffect(() => {
    setUnlinkingId(null);
    setConfirmUnmatch(false);
    setIsUnmatching(false);
    setViewerDocId(null);
  }, [transaction?.id]);

  const isPendingReview = transaction?.status === "PENDING_REVIEW";
  const doc = detail?.matched_document;

  async function handleUnmatchAll() {
    if (!transaction || !txnIdNum) return;
    const proofs = proofsData?.proofs ?? [];
    setIsUnmatching(true);
    try {
      // Remove every active proof. Each removal is awaited so server-side
      // counters and reconciliation_matches stay consistent.
      for (const p of proofs) {
        await removeProof.mutateAsync({ txnId: txnIdNum, proofId: p.proof_id });
      }
      onUnmatch?.(transaction);
      setConfirmUnmatch(false);
      onOpenChange(false);
    } finally {
      setIsUnmatching(false);
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-lg overflow-y-auto shadow-warm-md border-l-0"
      >
        <SheetHeader className="pb-4">
          <SheetTitle className="text-xl font-bold tracking-tight">
            Transaction Details
          </SheetTitle>
          <SheetDescription className="text-sm text-muted-foreground">
            {transaction?.description ?? "Loading..."}
          </SheetDescription>
        </SheetHeader>

        {isLoading ? (
          <div className="space-y-4 mt-6">
            <Skeleton className="h-4 w-full rounded-md" />
            <Skeleton className="h-4 w-3/4 rounded-md" />
            <Skeleton className="h-4 w-1/2 rounded-md" />
            <Skeleton className="h-32 w-full rounded-xl" />
          </div>
        ) : detail ? (
          <div className="mt-2 space-y-6">
            {/* Transaction Info */}
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                Transaction
              </h4>
              <div className="rounded-xl border border-border/60 bg-card p-4 shadow-warm-sm accent-border-left">
                <DetailRow label="Date" value={detail.date} />
                <DetailRow label="Description" value={detail.description} />
                <DetailRow
                  label="Amount"
                  value={formatCurrency(detail.amount, detail.currency)}
                  mono
                />
                <DetailRow label="Account" value={detail.account} />
                <DetailRow
                  label="Category"
                  value={
                    onCategoryChange && categoryGroups && categoryGroups.length > 0 && transaction ? (
                      <CategorySelect
                        value={detail.category}
                        groups={categoryGroups}
                        onChange={(next) => onCategoryChange(transaction, next)}
                        placeholder="Uncategorized"
                        className={cn(
                          "w-full rounded-md border border-border/60 bg-card px-2 py-1 text-sm",
                          "hover:border-primary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/30",
                          !detail.category && "text-muted-foreground italic"
                        )}
                        ariaLabel="Category"
                      />
                    ) : (
                      detail.category ?? "Uncategorized"
                    )
                  }
                />
                {detail.bank_reference && (
                  <DetailRow label="Reference" value={detail.bank_reference} />
                )}
                <DetailRow
                  label="Status"
                  value={
                    <Badge
                      className={cn(
                        "text-xs font-semibold",
                        detail.status === "MATCHED"
                          ? "bg-primary/10 text-primary border-primary/20"
                          : detail.status === "PENDING_REVIEW"
                          ? "bg-amber-100 text-amber-800 border-amber-200"
                          : "bg-muted text-muted-foreground border-border"
                      )}
                    >
                      {detail.status === "PENDING_REVIEW"
                        ? "Pending Review"
                        : detail.status}
                    </Badge>
                  }
                />
              </div>
            </div>

            {/* Notes & Supplementary Documents — always visible, including for IGNORED. */}
            {transaction && (
              <>
                <Separator className="bg-border/40" />
                <NotesSection
                  transactionId={parseInt(transaction.id, 10)}
                  initialNote={transaction.note ?? null}
                />
              </>
            )}

            {/* Transaction Linkage — a transaction may be linked to MANY
                counterparts (a bulk charge and each credit refunding part of
                it), so every leg is listed with its own remove control and
                the "link another" entry point never goes away. */}
            {transaction && (() => {
              const links = linksData?.links ?? [];
              const summary = linksData?.summary;
              const hasLinks = links.length > 0;
              return (
                <>
                  <Separator className="bg-border/40" />
                  <div>
                    <div className="flex items-center justify-between mb-3">
                      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                        Cross-Statement Linkage
                        {hasLinks && (
                          <span className="ml-1.5 font-mono normal-case text-muted-foreground/70">
                            ({links.length})
                          </span>
                        )}
                      </h4>
                      {hasLinks && onLinkTransaction && (
                        <button
                          onClick={() => onLinkTransaction(transaction)}
                          className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-950/30 transition-colors"
                        >
                          <ArrowLeftRight className="h-3.5 w-3.5" />
                          <span>Link another</span>
                        </button>
                      )}
                    </div>

                    {hasLinks ? (
                      <div className="space-y-2">
                        {summary && summary.offset_ratio !== null && parseFloat(summary.offset_total) > 0 && (
                          <div className="rounded-lg border border-indigo-200/50 bg-indigo-50/40 dark:border-indigo-800/40 dark:bg-indigo-950/20 px-3 py-2 text-xs">
                            <span className="font-mono tabular-nums font-medium">
                              {formatCurrency(Math.abs(parseFloat(summary.offset_total)), summary.transaction_currency)}
                            </span>{" "}
                            of{" "}
                            <span className="font-mono tabular-nums">
                              {formatCurrency(Math.abs(parseFloat(summary.transaction_amount)), summary.transaction_currency)}
                            </span>{" "}
                            {/* Count only the legs that actually contributed to
                                the sum — cross-currency and same-direction legs
                                are linked but deliberately not netted. */}
                            offset across {summary.offset_leg_count} linked transaction
                            {summary.offset_leg_count === 1 ? "" : "s"} —{" "}
                            <span className="font-mono tabular-nums font-medium">
                              {formatCurrency(Math.abs(parseFloat(summary.net_remaining)), summary.transaction_currency)}
                            </span>{" "}
                            net.
                            {summary.cross_currency_count > 0 && (
                              <span className="text-muted-foreground">
                                {" "}({summary.cross_currency_count} in another currency, not netted.)
                              </span>
                            )}
                            {summary.same_direction_count > 0 && (
                              <span className="text-muted-foreground">
                                {" "}({summary.same_direction_count} moving the same
                                direction, not netted.)
                              </span>
                            )}
                          </div>
                        )}
                        {links.map((link) => (
                          <div
                            key={link.id}
                            className={cn(
                              "flex items-start justify-between gap-2 rounded-lg border border-border/60 bg-card px-3 py-2",
                              unlinkingId === link.id && "opacity-50 pointer-events-none"
                            )}
                          >
                            <button
                              className="min-w-0 flex-1 text-left"
                              onClick={() => {
                                onOpenChange(false);
                                onChainNavigate?.(link.linked_txn_id, link.linked_source_file);
                              }}
                              title={link.linked_description}
                            >
                              <div className="flex items-center gap-2 text-sm">
                                <ArrowLeftRight className="h-3.5 w-3.5 shrink-0 text-indigo-500" />
                                <span className="truncate">{link.linked_description}</span>
                              </div>
                              <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                                <span>{link.linked_date}</span>
                                <span className="font-mono tabular-nums">
                                  {formatCurrency(Math.abs(parseFloat(link.linked_amount)), link.linked_currency)}
                                </span>
                                <span>{link.linked_account}</span>
                                <span className="text-indigo-500 text-[10px]">
                                  {link.link_type.replace(/_/g, " ")}
                                </span>
                              </div>
                            </button>
                            <button
                              onClick={() => {
                                setUnlinkingId(link.id);
                                deleteLinkMutation.mutate(link.id, {
                                  onSettled: () => setUnlinkingId(null),
                                });
                              }}
                              disabled={unlinkingId === link.id}
                              className="shrink-0 rounded-md p-1 text-destructive hover:bg-destructive/10 transition-colors disabled:opacity-50"
                              title="Remove this link"
                              aria-label={`Remove link to ${link.linked_description}`}
                            >
                              {unlinkingId === link.id ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <Trash2 className="h-3.5 w-3.5" />
                              )}
                            </button>
                          </div>
                        ))}
                        {/* A single link may continue into a longer transfer
                            chain (A→B→C); a fan-out group is already fully
                            listed above, and the linear strip would misread it. */}
                        {links.length === 1 && (
                          <TransactionChain
                            transactionId={parseInt(transaction.id, 10)}
                            onNavigate={(txnId, sourceFile) => {
                              onOpenChange(false);
                              onChainNavigate?.(txnId, sourceFile);
                            }}
                          />
                        )}
                      </div>
                    ) : (
                      <div className="rounded-xl border-2 border-dashed border-indigo-200/40 p-6 text-center bg-indigo-50/[0.03] dark:border-indigo-800/30 dark:bg-indigo-950/[0.05]">
                        <div className="mx-auto h-12 w-12 rounded-full bg-indigo-50 dark:bg-indigo-950/40 flex items-center justify-center mb-2.5">
                          <ArrowLeftRight className="h-6 w-6 text-indigo-400/50 dark:text-indigo-500/50" />
                        </div>
                        <p className="text-sm font-medium text-muted-foreground">
                          No linked transactions
                        </p>
                        <p className="text-xs text-muted-foreground/60 mt-0.5">
                          Link to related transactions — a transfer to the deposit it
                          lands in, or one bulk charge to every credit that refunds it.
                        </p>
                        {onLinkTransaction && (
                          <Button
                            variant="outline"
                            size="sm"
                            className="mt-3 border-indigo-300 text-indigo-600 hover:bg-indigo-50 dark:border-indigo-700 dark:text-indigo-400 dark:hover:bg-indigo-950/30"
                            onClick={() => onLinkTransaction(transaction)}
                          >
                            <ArrowLeftRight className="h-4 w-4 mr-1.5" />
                            Link Transaction
                          </Button>
                        )}
                      </div>
                    )}
                  </div>
                </>
              );
            })()}

            <Separator className="bg-border/40" />

            {/* Matched Document */}
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                Matched Receipt
              </h4>
              {(transaction?.status === "MATCHED" || transaction?.status === "PENDING_REVIEW") && (doc || (transaction?.proofs_count ?? 0) >= 1) ? (
                /* Unified proof bundle view — handles single and multi-proof with inline thumbnail */
                <ProofBundle
                  txnId={parseInt(transaction!.id, 10)}
                  txnAmount={detail.amount}
                  txnCurrency={detail.currency}
                  onAttachAnother={() => onAttachAnother?.(transaction!)}
                  onViewFile={(documentId) => setViewerDocId(documentId)}
                />
              ) : (
                <div className="rounded-xl border-2 border-dashed border-primary/15 p-8 text-center bg-primary/[0.02]">
                  <div className="mx-auto h-14 w-14 rounded-full bg-primary/5 flex items-center justify-center mb-3">
                    <FileText className="h-7 w-7 text-primary/25" />
                  </div>
                  <p className="text-sm font-medium text-muted-foreground">
                    No receipt matched to this transaction.
                  </p>
                  <p className="text-xs text-muted-foreground/60 mt-1">
                    Upload a receipt or run reconciliation to match.
                  </p>
                  {onMatchManually && transaction && (
                    <div className="flex gap-2 mt-4 justify-center">
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-primary/30 text-primary hover:bg-primary/5"
                        onClick={() => onMatchManually(transaction)}
                      >
                        <Link2 className="h-4 w-4 mr-1.5" />
                        Match Manually
                      </Button>
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Match Explanation */}
            {detail.match_score != null && (detail.match_explanation || detail.match_sub_scores) && (
              <>
                <Separator className="bg-border/40" />
                <div>
                  <div className="flex items-center justify-between mb-3">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Why this matched
                    </h4>
                    <ConfidenceBadge confidence={detail.match_score} />
                  </div>
                  <MatchExplanation
                    explanation={detail.match_explanation ?? "Matched based on amount, date, and vendor similarity."}
                    subScores={
                      detail.match_sub_scores
                        ? [
                            ...(detail.match_sub_scores.amount_score != null
                              ? [{ label: "Amount", score: detail.match_sub_scores.amount_score * 100, weight: 0.5, detail: "How closely the receipt total matches the transaction amount" }]
                              : []),
                            ...(detail.match_sub_scores.date_score != null
                              ? [{ label: "Date", score: detail.match_sub_scores.date_score * 100, weight: 0.3, detail: "How closely the receipt date matches the transaction date" }]
                              : []),
                            ...(detail.match_sub_scores.vendor_score != null
                              ? [{ label: "Vendor", score: detail.match_sub_scores.vendor_score * 100, weight: 0.2, detail: "How closely the receipt vendor matches the transaction description" }]
                              : []),
                          ]
                        : undefined
                    }
                    collapsible={false}
                  />
                </div>
              </>
            )}

            {/* Unmatch Action — visible whenever proofs are attached, including
                PENDING_REVIEW and multi-proof bundles where status alone may
                not be MATCHED. */}
            {transaction && (proofsData?.proofs.length ?? 0) > 0 && (
              <>
                <Separator className="bg-border/40" />
                <div>
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                    Unmatch
                  </h4>
                  {!confirmUnmatch ? (
                    <Button
                      variant="outline"
                      onClick={() => setConfirmUnmatch(true)}
                      disabled={isUnmatching}
                      className="w-full border-destructive/30 text-destructive hover:bg-destructive/5 hover:border-destructive/50 rounded-lg"
                    >
                      <Unlink className="h-4 w-4 mr-1.5" />
                      {(proofsData?.proofs.length ?? 0) > 1
                        ? `Unmatch All ${proofsData?.proofs.length} Receipts`
                        : "Unmatch Receipt"}
                    </Button>
                  ) : (
                    <div className="rounded-xl border border-destructive/20 bg-destructive/[0.03] p-4 space-y-3">
                      <p className="text-sm text-destructive dark:text-red-300">
                        Are you sure? This will remove every receipt currently
                        attached to this transaction.
                      </p>
                      <div className="flex gap-3">
                        <Button
                          variant="destructive"
                          size="sm"
                          className="flex-1 rounded-lg"
                          onClick={handleUnmatchAll}
                          disabled={isUnmatching}
                        >
                          {isUnmatching ? (
                            <>
                              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                              Unmatching…
                            </>
                          ) : (
                            "Yes, unmatch"
                          )}
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          className="flex-1 rounded-lg"
                          onClick={() => setConfirmUnmatch(false)}
                          disabled={isUnmatching}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              </>
            )}

            {/* Review Actions */}
            {isPendingReview && (
              <>
                <Separator className="bg-border/40" />
                <div>
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                    Review Actions
                  </h4>
                  <div className="rounded-xl border border-amber-200/60 bg-amber-50/30 p-4 mb-4 dark:bg-amber-950/10 dark:border-amber-800/40">
                    <p className="text-sm text-amber-800 dark:text-amber-200">
                      This match was flagged for manual review. Approve to
                      confirm the match or reject to unmatch.
                    </p>
                  </div>
                  <SheetFooter className="flex gap-3 sm:flex-row">
                    <Button
                      onClick={() =>
                        reviewMutation.mutate({ action: "approve" })
                      }
                      disabled={reviewMutation.isPending}
                      className="flex-1 bg-primary hover:bg-primary/90 text-primary-foreground rounded-lg shadow-warm-sm"
                    >
                      {reviewMutation.isPending ? (
                        <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                      ) : (
                        <CheckCircle className="h-4 w-4 mr-1.5" />
                      )}
                      Approve Match
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() =>
                        reviewMutation.mutate({ action: "reject" })
                      }
                      disabled={reviewMutation.isPending}
                      className="flex-1 border-destructive/30 text-destructive hover:bg-destructive/5 hover:border-destructive/50 rounded-lg"
                    >
                      {reviewMutation.isPending ? (
                        <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                      ) : (
                        <XCircle className="h-4 w-4 mr-1.5" />
                      )}
                      Reject Match
                    </Button>
                  </SheetFooter>
                </div>
              </>
            )}
          </div>
        ) : null}
      </SheetContent>
      <ReceiptViewerModal
        documentId={viewerDocId}
        open={viewerDocId !== null}
        onOpenChange={(open) => {
          if (!open) setViewerDocId(null);
        }}
      />
    </Sheet>
  );
}
