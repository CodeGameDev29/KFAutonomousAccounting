import { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { getErrorMessage, getErrorMessageFromResponse } from "@/lib/error-messages";
import { useAuthStore } from "@/stores/auth-store";
import { cn } from "@/lib/utils";
import { displayLabel, MATCH_METHOD_LABELS } from "@/lib/display-labels";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { ConfidenceIndicator } from "@/components/ui/confidence-indicator";
import { MatchExplanation } from "@/components/MatchExplanation";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  CheckCircle,
  XCircle,
  Unlink,
  Pencil,
  Save,
  X,
  FileText,
  ExternalLink,
  Loader2,
  ImageIcon,
  ArrowLeftRight,
} from "lucide-react";
import { ReceiptSelectorDialog } from "@/components/transactions/ReceiptSelectorDialog";

// -- Types -------------------------------------------------------------------

export interface ReviewTransaction {
  id: number;
  transaction_id: number;
  document_id: number;
  confidence_score: number;
  amount_score: number | null;
  date_score: number | null;
  vendor_score: number | null;
  match_type: string;
  status: string;
  explanation: string | null;
  user_action: string | null;
  actioned_at: string | null;
  reviewed_by: string | null;
  created_at: string | null;
  transaction: {
    description: string;
    date: string | null;
    amount: string | null;
    currency: string;
    account: string;
  };
  document: {
    vendor: string;
    date: string | null;
    total: string | null;
    currency: string;
    filename: string;
    stored_path: string | null;
  };
}

interface MatchesResponse {
  matches: ReviewTransaction[];
  total: number;
  page: number;
  per_page: number;
  queue_counts: Record<string, number>;
}

interface ReceiptReviewPanelProps {
  transaction: ReviewTransaction | null;
  onApprove?: (id: number) => void;
  onReject?: (id: number) => void;
  onUnmatch?: (id: number) => void;
  showActions?: boolean;
  activeTab?: string;
}

// -- Helpers -----------------------------------------------------------------

function formatCurrency(amount: string | null, currency: string): string {
  if (!amount) return "--";
  const num = parseFloat(amount);
  if (isNaN(num)) return amount;
  return num.toLocaleString("en-CA", {
    style: "currency",
    currency: currency === "CAD" || currency === "USD" ? currency : "CAD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "--";
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-CA", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function isImageFile(filename: string): boolean {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  return ["jpg", "jpeg", "png", "webp", "jfif", "gif", "bmp", "tiff", "tif"].includes(ext);
}

function isPdfFile(filename: string): boolean {
  return filename.toLowerCase().endsWith(".pdf");
}

function isPreviewableFile(filename: string): boolean {
  return isPdfFile(filename) || isImageFile(filename);
}

function MatchTypeBadge({ matchType }: { matchType: string }) {
  const normalized = matchType?.toLowerCase().replace("_", " ") ?? "standard";
  let colorClass: string;
  if (normalized.includes("strict")) {
    colorClass = "bg-primary/10 text-primary border-primary/20";
  } else if (normalized.includes("relax")) {
    colorClass =
      "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800";
  } else if (normalized.includes("aggressive")) {
    colorClass = "bg-destructive/10 text-destructive border-destructive/20";
  } else {
    colorClass = "bg-muted text-muted-foreground border-border";
  }

  return (
    <Badge className={cn("text-[10px] font-semibold uppercase tracking-wider", colorClass)}>
      {displayLabel(matchType, MATCH_METHOD_LABELS)}
    </Badge>
  );
}

function DetailRow({
  label,
  value,
  mono,
  confidence,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  confidence?: number;
}) {
  return (
    <div className="flex justify-between items-start py-2 border-b border-border/30 last:border-0">
      <span className="text-xs font-medium uppercase tracking-wider text-primary/60">
        {label}
      </span>
      <div className="flex items-center gap-2">
        {confidence !== undefined && (
          <ConfidenceIndicator confidence={confidence} variant="compact" />
        )}
        <span
          className={cn(
            "text-sm font-medium text-right max-w-[55%]",
            mono && "font-mono"
          )}
        >
          {value}
        </span>
      </div>
    </div>
  );
}

// -- Component ---------------------------------------------------------------

export function ReceiptReviewPanel({
  transaction,
  onApprove,
  onReject,
  onUnmatch,
  showActions = true,
  activeTab,
}: ReceiptReviewPanelProps) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [unmatchDialogOpen, setUnmatchDialogOpen] = useState(false);
  const [reassignDialogOpen, setReassignDialogOpen] = useState(false);
  // Snapshot of the transaction being reassigned. The optimistic reject
  // removes the row from the review-matches cache in the same click, so the
  // live `transaction` prop slides to the NEXT item before the dialog opens
  // — rendering the dialog from the prop would reassign the wrong txn.
  const [reassignTarget, setReassignTarget] = useState<ReviewTransaction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [editFields, setEditFields] = useState({
    vendor: "",
    date: "",
    total: "",
    currency: "",
  });

  // Document view/download error
  const [viewError, setViewError] = useState<string | null>(null);

  // Fetch preview PNG from server (works for all file types: PDF, images, docx, xlsx, csv)
  const [previewBlobUrl, setPreviewBlobUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewFailed, setPreviewFailed] = useState<string | null>(null);
  const docId = transaction?.document_id;
  useEffect(() => {
    if (!docId) {
      setPreviewBlobUrl(null);
      setPreviewLoading(false);
      setPreviewFailed(null);
      return;
    }
    let revoked = false;
    setPreviewLoading(true);
    setPreviewFailed(null);
    (async () => {
      const fetchPreview = async (retry = false) => {
        const { getAccessToken } = useAuthStore.getState();
        const token = await getAccessToken();
        const res = await fetch(`${API_URL}/api/receipts/${docId}/preview`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (revoked) return;
        // Auto-retry on 403/410 (expired signed URL)
        if ((res.status === 403 || res.status === 410) && !retry) {
          return fetchPreview(true);
        }
        if (!res.ok) {
          setPreviewLoading(false);
          setPreviewFailed(getErrorMessageFromResponse(res, "loading preview"));
          return;
        }
        const blob = await res.blob();
        if (revoked) return;
        setPreviewBlobUrl(URL.createObjectURL(blob));
        setPreviewLoading(false);
      };
      try {
        await fetchPreview();
      } catch (err) {
        if (!revoked) {
          setPreviewLoading(false);
          setPreviewFailed(getErrorMessage(err, "loading preview"));
        }
      }
    })();
    return () => {
      revoked = true;
      setPreviewBlobUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return null;
      });
    };
  }, [docId]);

  // -- Mutations --------------------------------------------------------------

  const approveMutation = useMutation({
    mutationFn: (matchId: number) =>
      api.patch(`/api/reconciliation/matches/${matchId}/approve`),
    onMutate: async (matchId: number) => {
      setActionError(null);
      setActionSuccess(null);
      // Cancel in-flight review-matches queries to avoid overwriting optimistic update
      await queryClient.cancelQueries({ queryKey: ["review-matches"] });
      // Snapshot all review-matches query data for rollback
      const prevData = queryClient.getQueriesData<MatchesResponse>({
        queryKey: ["review-matches"],
      });
      // Optimistically remove the match from all review-matches caches and update counts
      queryClient.setQueriesData<MatchesResponse>(
        { queryKey: ["review-matches"] },
        (old) => {
          if (!old) return old;
          const removed = old.matches.find((m) => m.id === matchId);
          const filtered = old.matches.filter((m) => m.id !== matchId);
          const newCounts = { ...old.queue_counts };
          if (removed) {
            // Decrement the source status count
            const srcStatus = removed.status;
            if (newCounts[srcStatus] !== undefined) {
              newCounts[srcStatus] = Math.max(0, newCounts[srcStatus] - 1);
            }
            // Increment USER_APPROVED count
            newCounts["USER_APPROVED"] = (newCounts["USER_APPROVED"] ?? 0) + 1;
          }
          return {
            ...old,
            matches: filtered,
            total: Math.max(0, old.total - (removed ? 1 : 0)),
            queue_counts: newCounts,
          };
        }
      );
      setActionSuccess("Match approved");
      toast.success("Match approved.");
      setTimeout(() => setActionSuccess(null), 2000);
      onApprove?.(matchId);
      return { prevData };
    },
    onError: (err, _matchId, context) => {
      // Rollback to snapshot
      const ctx = context as { prevData?: [readonly unknown[], MatchesResponse | undefined][] } | undefined;
      if (ctx?.prevData) {
        ctx.prevData.forEach(([key, data]) => queryClient.setQueryData(key, data));
      }
      setActionSuccess(null);
      setActionError(getErrorMessage(err, "approving this match"));
      toast.error(getErrorMessage(err, "approving this match"));
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["review-matches"] });
      queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const rejectMutation = useMutation({
    mutationFn: (matchId: number) =>
      api.patch(`/api/reconciliation/matches/${matchId}/reject`),
    onMutate: async (matchId: number) => {
      setActionError(null);
      setActionSuccess(null);
      await queryClient.cancelQueries({ queryKey: ["review-matches"] });
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
            newCounts["USER_REJECTED"] = (newCounts["USER_REJECTED"] ?? 0) + 1;
          }
          return {
            ...old,
            matches: filtered,
            total: Math.max(0, old.total - (removed ? 1 : 0)),
            queue_counts: newCounts,
          };
        }
      );
      setActionSuccess("Match rejected");
      toast.success("Match rejected.");
      setTimeout(() => setActionSuccess(null), 2000);
      onReject?.(matchId);
      return { prevData };
    },
    onError: (err, _matchId, context) => {
      const ctx = context as { prevData?: [readonly unknown[], MatchesResponse | undefined][] } | undefined;
      if (ctx?.prevData) {
        ctx.prevData.forEach(([key, data]) => queryClient.setQueryData(key, data));
      }
      setActionSuccess(null);
      setActionError(getErrorMessage(err, "rejecting this match"));
      toast.error(getErrorMessage(err, "rejecting this match"));
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["review-matches"] });
      queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const unmatchMutation = useMutation({
    mutationFn: (matchId: number) =>
      api.post(`/api/reconciliation/matches/${matchId}/unmatch`),
    onMutate: async (matchId: number) => {
      setActionError(null);
      setActionSuccess(null);
      await queryClient.cancelQueries({ queryKey: ["review-matches"] });
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
            newCounts["UNMATCHED"] = (newCounts["UNMATCHED"] ?? 0) + 1;
          }
          return {
            ...old,
            matches: filtered,
            total: Math.max(0, old.total - (removed ? 1 : 0)),
            queue_counts: newCounts,
          };
        }
      );
      setUnmatchDialogOpen(false);
      setActionSuccess("Match removed");
      toast.success("Match removed — the transaction is unmatched again.");
      setTimeout(() => setActionSuccess(null), 2000);
      onUnmatch?.(matchId);
      return { prevData };
    },
    onError: (err, _matchId, context) => {
      const ctx = context as { prevData?: [readonly unknown[], MatchesResponse | undefined][] } | undefined;
      if (ctx?.prevData) {
        ctx.prevData.forEach(([key, data]) => queryClient.setQueryData(key, data));
      }
      setActionSuccess(null);
      setActionError(getErrorMessage(err, "removing this match"));
      toast.error(getErrorMessage(err, "removing this match"));
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["review-matches"] });
      queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const correctMutation = useMutation({
    mutationFn: ({
      docId,
      fields,
    }: {
      docId: number;
      fields: Record<string, string>;
    }) => api.patch(`/api/receipts/${docId}/correct`, fields),
    onMutate: () => {
      setActionError(null);
      setActionSuccess(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["review-matches"] });
      queryClient.invalidateQueries({ queryKey: ["review-match-counts"] });
      setEditing(false);
      setActionSuccess("Match updated");
      toast.success("Receipt details saved.");
      setTimeout(() => setActionSuccess(null), 2000);
    },
    onError: (err) => {
      setActionError(getErrorMessage(err, "updating this match"));
    },
  });

  const isMutating =
    approveMutation.isPending ||
    rejectMutation.isPending ||
    unmatchMutation.isPending;

  // -- Empty state ------------------------------------------------------------

  if (!transaction) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center">
        <div className="space-y-3">
          <div className="mx-auto h-16 w-16 rounded-full bg-primary/5 flex items-center justify-center">
            <FileText className="h-8 w-8 text-primary/20" />
          </div>
          <p className="text-sm font-medium text-muted-foreground">
            Select a transaction to review
          </p>
          <p className="text-xs text-muted-foreground/50 max-w-[200px] mx-auto">
            Click on a transaction from the list to see its matched receipt
            details.
          </p>
        </div>
      </div>
    );
  }

  // Captured after the null guard above: the callbacks defined below outlive
  // TypeScript's narrowing of the `transaction` prop, so they close over this
  // instead of re-reading a value the compiler can no longer prove is set.
  const reviewed = transaction;
  const { document: doc, transaction: txn } = transaction;
  const confidencePercent = transaction.confidence_score * 100;

  // Build sub-scores from the match data
  const subScores = [];
  if (transaction.amount_score !== null) {
    subScores.push({
      label: "Amount",
      score: transaction.amount_score * 100,
      weight: 0.5,
      detail: `Bank: ${formatCurrency(txn.amount, txn.currency)} vs Proof of Transaction: ${formatCurrency(doc.total, doc.currency)}`,
    });
  }
  if (transaction.date_score !== null) {
    subScores.push({
      label: "Date",
      score: transaction.date_score * 100,
      weight: 0.3,
      detail: `Bank: ${formatDate(txn.date)} vs Proof of Transaction: ${formatDate(doc.date)}`,
    });
  }
  if (transaction.vendor_score !== null) {
    subScores.push({
      label: "Vendor",
      score: transaction.vendor_score * 100,
      weight: 0.2,
      detail: `Bank description matched to "${doc.vendor}"`,
    });
  }

  const filename = doc.filename ?? "";
  const fileUrl = transaction.document_id
    ? `/api/receipts/${transaction.document_id}/download`
    : null;

  function startEditing() {
    setEditFields({
      vendor: doc.vendor ?? "",
      date: doc.date ?? "",
      total: doc.total ?? "",
      // Empty, never "CAD": a receipt that never stated a currency must not be
      // handed one by the editor. The server stamps any currency it receives as
      // currency_source='stated', so a default here would put the user's name
      // on a guess they never made.
      currency: doc.currency ?? "",
    });
    setEditing(true);
  }

  function handleSaveCorrection() {
    const fields: Record<string, string> = {};
    if (editFields.vendor && editFields.vendor !== doc.vendor)
      fields.vendor = editFields.vendor;
    if (editFields.date && editFields.date !== doc.date)
      fields.date = editFields.date;
    if (editFields.total && editFields.total !== doc.total)
      fields.total = editFields.total;
    // Only a currency the user actually typed is sent; leaving the box empty
    // (or unchanged) says nothing, and silence is what a receipt that never
    // stated a currency deserves.
    const currency = editFields.currency.trim();
    if (currency && currency !== (doc.currency ?? "")) fields.currency = currency;

    if (Object.keys(fields).length === 0) {
      setEditing(false);
      return;
    }

    correctMutation.mutate({
      docId: reviewed.document_id,
      fields,
    });
  }

  // Determine if this is an actionable status
  const canApproveReject =
    transaction.status === "AUTO_APPROVED" ||
    transaction.status === "PENDING_REVIEW" ||
    transaction.status === "NEEDS_REVIEW";
  const canUnmatch =
    transaction.status === "USER_APPROVED" ||
    transaction.status === "AUTO_APPROVED";

  return (
    <div className="flex flex-col h-full overflow-y-auto">
      {/* Receipt Preview */}
      <div className="border-b border-border/40 bg-muted/20 p-4">
        {previewBlobUrl ? (
          <img
            src={previewBlobUrl}
            alt="Receipt preview"
            className="w-full h-48 object-contain rounded-xl border border-border/40 bg-white shadow-warm-sm cursor-pointer"
            onClick={() => window.open(previewBlobUrl, "_blank", "noopener,noreferrer")}
            title="Click to open full size"
          />
        ) : previewLoading ? (
          <div className="flex h-48 items-center justify-center rounded-xl border border-border/40 bg-white shadow-warm-sm">
            <Loader2 className="h-5 w-5 animate-spin text-primary/30" />
          </div>
        ) : (
          <div className="flex h-48 items-center justify-center rounded-xl border-2 border-dashed border-primary/15 bg-primary/[0.02]">
            <div className="text-center">
              <div className="mx-auto h-12 w-12 rounded-full bg-primary/5 flex items-center justify-center mb-2">
                <ImageIcon className="h-5 w-5 text-primary/25" />
              </div>
              <p className="text-xs text-muted-foreground/50">
                {previewFailed ? previewFailed : "No preview available"}
              </p>
            </div>
          </div>
        )}
        {fileUrl && (
          <div className="mt-3">
            <Button
              variant="outline"
              size="sm"
              className="w-full border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50 rounded-lg"
              onClick={async () => {
                setViewError(null);
                try {
                  const { getAccessToken } = useAuthStore.getState();
                  const token = await getAccessToken();
                  const res = await fetch(`${API_URL}${fileUrl}`, {
                    headers: token ? { Authorization: `Bearer ${token}` } : {},
                    redirect: "follow",
                  });
                  if (res.ok) {
                    const blob = await res.blob();
                    const docFilename = doc.filename || "document";
                    if (isPreviewableFile(docFilename)) {
                      // PDFs and images: open in new browser tab
                      const url = URL.createObjectURL(blob);
                      window.open(url, "_blank", "noopener,noreferrer");
                      setTimeout(() => URL.revokeObjectURL(url), 60_000);
                    } else {
                      // Non-viewable types (docx, xlsx, etc.): download with correct filename
                      const typedBlob = new Blob([blob], {
                        type: res.headers.get("content-type") || blob.type || "application/octet-stream",
                      });
                      const url = URL.createObjectURL(typedBlob);
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = docFilename;
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                      setTimeout(() => URL.revokeObjectURL(url), 5_000);
                    }
                  } else {
                    setViewError(getErrorMessageFromResponse(res, "loading this document"));
                  }
                } catch (err) {
                  setViewError(getErrorMessage(err, "loading this document"));
                }
              }}
            >
              <FileText className="h-3.5 w-3.5 mr-1.5" />
              {isPreviewableFile(filename) ? "Open document" : `Download ${filename.split(".").pop()?.toUpperCase() || "file"}`}
              <ExternalLink className="h-3 w-3 ml-1.5" />
            </Button>
            {viewError && (
              <span className="text-xs text-amber-700 block mt-1.5">
                {viewError}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Extracted Data */}
      <div className="flex-1 p-4 space-y-5">
        {/* Transaction info */}
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
            Bank Transaction
          </h4>
          <div className="rounded-xl border border-border/40 bg-card p-4 shadow-warm-sm accent-border-left">
            <DetailRow label="Date" value={formatDate(txn.date)} />
            <DetailRow label="Description" value={txn.description} />
            <DetailRow
              label="Amount"
              value={formatCurrency(txn.amount, txn.currency)}
              mono
            />
            <DetailRow label="Account" value={txn.account} />
          </div>
        </div>

        {/* Matched receipt info */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Matched Receipt
            </h4>
            {!editing && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs text-muted-foreground hover:text-primary hover:bg-primary/5"
                onClick={startEditing}
              >
                <Pencil className="h-3 w-3 mr-1" />
                Edit
              </Button>
            )}
          </div>

          {editing ? (
            <div className="rounded-xl border border-primary/20 bg-primary/[0.02] p-4 space-y-3 shadow-warm-sm">
              <div className="space-y-1.5">
                <Label
                  htmlFor="edit-vendor"
                  className="text-xs font-medium uppercase tracking-wider text-primary/60"
                >
                  Vendor
                </Label>
                <Input
                  id="edit-vendor"
                  value={editFields.vendor}
                  onChange={(e) =>
                    setEditFields({ ...editFields, vendor: e.target.value })
                  }
                  className="h-8 text-sm rounded-lg border-border/60 focus-visible:ring-primary/30"
                />
              </div>
              <div className="space-y-1.5">
                <Label
                  htmlFor="edit-date"
                  className="text-xs font-medium uppercase tracking-wider text-primary/60"
                >
                  Date
                </Label>
                <Input
                  id="edit-date"
                  type="date"
                  value={editFields.date}
                  onChange={(e) =>
                    setEditFields({ ...editFields, date: e.target.value })
                  }
                  className="h-8 text-sm rounded-lg border-border/60 focus-visible:ring-primary/30"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label
                    htmlFor="edit-total"
                    className="text-xs font-medium uppercase tracking-wider text-primary/60"
                  >
                    Total
                  </Label>
                  <Input
                    id="edit-total"
                    value={editFields.total}
                    onChange={(e) =>
                      setEditFields({ ...editFields, total: e.target.value })
                    }
                    className="h-8 text-sm font-mono rounded-lg border-border/60 focus-visible:ring-primary/30"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label
                    htmlFor="edit-currency"
                    className="text-xs font-medium uppercase tracking-wider text-primary/60"
                  >
                    Currency
                  </Label>
                  <Input
                    id="edit-currency"
                    value={editFields.currency}
                    placeholder="Not stated"
                    onChange={(e) =>
                      setEditFields({ ...editFields, currency: e.target.value })
                    }
                    className="h-8 text-sm rounded-lg border-border/60 focus-visible:ring-primary/30"
                  />
                </div>
              </div>
              <div className="flex gap-2 pt-1">
                <Button
                  size="sm"
                  className="flex-1 bg-primary hover:bg-primary/90 rounded-lg shadow-warm-sm"
                  onClick={handleSaveCorrection}
                  disabled={correctMutation.isPending}
                >
                  {correctMutation.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                  ) : (
                    <Save className="h-3.5 w-3.5 mr-1" />
                  )}
                  Save
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditing(false)}
                  className="rounded-lg"
                >
                  <X className="h-3.5 w-3.5 mr-1" />
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-border/40 bg-card p-4 shadow-warm-sm accent-border-left">
              <DetailRow label="Vendor" value={doc.vendor ?? "--"} />
              <DetailRow label="Date" value={formatDate(doc.date)} />
              <DetailRow
                label="Total"
                value={formatCurrency(doc.total, doc.currency)}
                mono
              />
              <DetailRow label="Currency" value={doc.currency ?? "--"} />
              <DetailRow label="File" value={doc.filename ?? "--"} />
            </div>
          )}
        </div>

        <Separator className="bg-border/30" />

        {/* Match Explanation */}
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
            Match Analysis
          </h4>
          <div className="flex items-center gap-2.5 mb-3">
            <ConfidenceIndicator
              confidence={confidencePercent}
              variant="expanded"
            />
            <MatchTypeBadge matchType={transaction.match_type} />
          </div>
          <MatchExplanation
            explanation={
              transaction.explanation ??
              "No explanation available for this match."
            }
            subScores={subScores.length > 0 ? subScores : undefined}
            collapsible={activeTab === "auto_approved"}
          />
        </div>
      </div>

      {/* Action Buttons */}
      {showActions && (
        <div className="border-t border-border/40 p-4 space-y-2.5 bg-card/80 backdrop-blur-sm">
          {canApproveReject && (
            <>
              <div className="flex gap-2.5">
                <Button
                  className="flex-1 bg-primary hover:bg-primary/90 text-primary-foreground rounded-lg shadow-warm-sm"
                  onClick={() => approveMutation.mutate(transaction.id)}
                  disabled={isMutating}
                >
                  {approveMutation.isPending ? (
                    <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                  ) : (
                    <CheckCircle className="h-4 w-4 mr-1.5" />
                  )}
                  Approve
                </Button>
                <Button
                  variant="outline"
                  className="flex-1 border-destructive/30 text-destructive hover:bg-destructive/5 hover:border-destructive/50 rounded-lg"
                  onClick={() => rejectMutation.mutate(transaction.id)}
                  disabled={isMutating}
                >
                  {rejectMutation.isPending ? (
                    <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                  ) : (
                    <XCircle className="h-4 w-4 mr-1.5" />
                  )}
                  Reject
                </Button>
              </div>
              <Button
                variant="outline"
                className="w-full border-primary/30 text-primary hover:bg-primary/5 hover:border-primary/50 rounded-lg"
                onClick={() => {
                  setReassignTarget(transaction);
                  rejectMutation.mutate(transaction.id);
                  setReassignDialogOpen(true);
                }}
                disabled={isMutating}
              >
                <ArrowLeftRight className="h-4 w-4 mr-1.5" />
                Reject &amp; Reassign Receipt
              </Button>
            </>
          )}
          {canUnmatch && (
            <Button
              variant="ghost"
              className="w-full text-destructive/70 hover:text-destructive hover:bg-destructive/5 rounded-lg"
              onClick={() => setUnmatchDialogOpen(true)}
              disabled={isMutating}
            >
              <Unlink className="h-4 w-4 mr-1.5" />
              Unmatch
            </Button>
          )}
          <div aria-live="polite" role="status">
            {actionError && (
              <p className="mt-2 text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
                {actionError}
              </p>
            )}
            {actionSuccess && (
              <p className="mt-2 text-sm text-success bg-success/10 px-3 py-2 rounded-lg flex items-center gap-1.5">
                <CheckCircle className="h-3.5 w-3.5" />
                {actionSuccess}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Unmatch Confirmation Dialog */}
      <Dialog open={unmatchDialogOpen} onOpenChange={setUnmatchDialogOpen}>
        <DialogContent className="rounded-xl shadow-warm-md">
          <DialogHeader>
            <DialogTitle className="text-lg font-bold">
              Unmatch this transaction?
            </DialogTitle>
            <DialogDescription className="text-sm text-muted-foreground">
              This will remove the link between the bank transaction and the
              matched receipt. Both items will return to the unmatched queue and
              can be re-matched during the next reconciliation run.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              onClick={() => setUnmatchDialogOpen(false)}
              className="rounded-lg"
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => unmatchMutation.mutate(transaction.id)}
              disabled={unmatchMutation.isPending}
              className="rounded-lg shadow-warm-sm"
            >
              {unmatchMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
              ) : (
                <Unlink className="h-4 w-4 mr-1.5" />
              )}
              Confirm Unmatch
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Reassign Receipt Dialog — rendered from the snapshot captured at
          click time, NOT the live prop (see reassignTarget comment). */}
      {reassignTarget && (
        <ReceiptSelectorDialog
          open={reassignDialogOpen}
          onOpenChange={(open) => {
            setReassignDialogOpen(open);
            if (!open) setReassignTarget(null);
          }}
          transaction={{
            id: String(reassignTarget.transaction_id),
            description: reassignTarget.transaction.description,
            amount: parseFloat(reassignTarget.transaction.amount ?? "0"),
            date: reassignTarget.transaction.date ?? "",
            currency: reassignTarget.transaction.currency,
            category: null,
            status: "UNMATCHED",
            account: reassignTarget.transaction.account,
            matched_doc_id: null,
            matched_doc_filename: null,
            match_id: null,
            confidence: null,
            source_file: null,
            note: null,
          }}
          existingMatchId={null}
          onMatchSuccess={() => {
            setReassignDialogOpen(false);
            setReassignTarget(null);
            queryClient.invalidateQueries({ queryKey: ["review-matches"] });
          }}
        />
      )}
    </div>
  );
}
