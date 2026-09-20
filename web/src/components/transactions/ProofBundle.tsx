import { useCallback, useEffect, useRef, useState } from "react";
import { FileCheck, Paperclip, Trash2, Eye, Undo2, Loader2, ImageIcon } from "lucide-react";
import { useTransactionProofs, useRemoveProof, useAddProof } from "@/hooks/use-transaction-proofs";
import { CoverageIndicator } from "./CoverageIndicator";
import { cn } from "@/lib/utils";
import { API_URL } from "@/lib/constants";
import { useAuthStore } from "@/stores/auth-store";

function ReceiptThumbnail({ documentId, onClick }: { documentId: number; onClick: () => void }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const token = await useAuthStore.getState().getAccessToken();
        const res = await fetch(`${API_URL}/api/receipts/${documentId}/preview`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok || cancelled) return;
        const blob = await res.blob();
        if (cancelled) return;
        setSrc(URL.createObjectURL(blob));
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => { cancelled = true; };
  }, [documentId]);

  if (failed || !src) {
    return (
      <button
        onClick={onClick}
        className="shrink-0 h-12 w-12 rounded-lg bg-muted/40 flex items-center justify-center hover:bg-muted/60 transition-colors"
        title="View receipt"
      >
        <ImageIcon className="h-5 w-5 text-muted-foreground/40" />
      </button>
    );
  }

  return (
    <button
      onClick={onClick}
      className="shrink-0 h-12 w-12 rounded-lg overflow-hidden border border-border/40 hover:border-primary/40 transition-colors"
      title="View receipt"
    >
      <img src={src} alt="Receipt preview" className="h-full w-full object-cover" />
    </button>
  );
}

interface ProofBundleProps {
  txnId: number;
  txnAmount: number;
  txnCurrency: string;
  onAttachAnother: () => void;
  onViewFile: (documentId: number) => void;
}

interface UndoState {
  proofId: number;
  docId: number;
  filename: string;
}

export function ProofBundle({
  txnId,
  txnAmount,
  txnCurrency,
  onAttachAnother,
  onViewFile,
}: ProofBundleProps) {
  const { data, isLoading, isFetching } = useTransactionProofs(txnId);
  const removeProof = useRemoveProof();
  const addProof = useAddProof();
  const [undoState, setUndoState] = useState<UndoState | null>(null);
  const [removingProofId, setRemovingProofId] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const handleRemove = useCallback(
    (proofId: number, docId: number, filename: string) => {
      setRemovingProofId(proofId);
      removeProof.mutate(
        { txnId, proofId },
        {
          onSuccess: () => {
            setRemovingProofId(null);
            setUndoState({ proofId, docId, filename });
            if (timerRef.current) clearTimeout(timerRef.current);
            timerRef.current = setTimeout(() => setUndoState(null), 6000);
          },
          onError: () => {
            setRemovingProofId(null);
          },
        },
      );
    },
    [txnId, removeProof],
  );

  const handleUndo = useCallback(() => {
    if (!undoState) return;
    addProof.mutate(
      { txnId, documentIds: [undoState.docId] },
      { onSuccess: () => setUndoState(null) },
    );
    if (timerRef.current) clearTimeout(timerRef.current);
  }, [txnId, undoState, addProof]);

  if (isLoading || !data) {
    return <div className="animate-pulse h-24 bg-muted/20 rounded-lg" />;
  }

  const proofs = data.proofs;
  const coverage = data.coverage;
  const coveredAmount = parseFloat(coverage.total_proof_amount) || 0;
  const coveragePct = coverage.coverage_ratio;

  const isRefetching = isFetching && !isLoading;

  return (
    <div className={cn("space-y-3 relative", isRefetching && "opacity-60 pointer-events-none")}>
      {isRefetching && (
        <div className="absolute inset-0 flex items-center justify-center z-10">
          <Loader2 className="h-5 w-5 animate-spin text-primary" />
        </div>
      )}
      {/* Header */}
      <h4 className="text-sm font-medium text-foreground">
        Matched Receipts ({proofs.length})
      </h4>

      {/* Coverage indicator */}
      {proofs.length > 1 && (
        <CoverageIndicator
          coveredAmount={coveredAmount}
          totalAmount={Math.abs(txnAmount)}
          currency={txnCurrency}
          coveragePct={coveragePct}
          crossCurrencyCount={coverage.cross_currency_count ?? 0}
        />
      )}

      {/* Proof cards */}
      <div className="space-y-2">
        {proofs.map((proof) => (
          <div
            key={proof.proof_id}
            className="flex items-center justify-between rounded-lg border bg-card p-3"
          >
            <div className="flex items-center gap-2 min-w-0">
              {proof.document_id != null ? (
                <ReceiptThumbnail
                  documentId={proof.document_id}
                  onClick={() => onViewFile(proof.document_id!)}
                />
              ) : (
                <FileCheck className="h-4 w-4 shrink-0 text-green-600" />
              )}
              <div className="min-w-0">
                <p className="text-sm font-medium truncate">{proof.label}</p>
                <p className="text-xs text-muted-foreground">
                  {[proof.vendor, proof.date, proof.amount ? `$${proof.amount}` : null]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              {proof.confidence_score != null && (
                <span
                  className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
                    proof.confidence_score >= 0.8
                      ? "bg-green-100 text-green-700"
                      : proof.confidence_score >= 0.6
                        ? "bg-amber-100 text-amber-700"
                        : "bg-red-100 text-red-700"
                  }`}
                >
                  {Math.round(proof.confidence_score * 100)}%
                </span>
              )}
              {proof.document_id != null && (
                <button
                  onClick={() => onViewFile(proof.document_id!)}
                  className="p-1 rounded hover:bg-muted/50 text-muted-foreground hover:text-foreground"
                  title="View file"
                >
                  <Eye className="h-3.5 w-3.5" />
                </button>
              )}
              <button
                onClick={() => handleRemove(proof.proof_id, 0, proof.label)}
                disabled={removingProofId === proof.proof_id}
                className="p-1 rounded hover:bg-red-50 text-muted-foreground hover:text-red-600 disabled:opacity-50"
                title="Remove proof"
              >
                {removingProofId === proof.proof_id ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Trash2 className="h-3.5 w-3.5" />
                )}
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Attach another button */}
      <button
        onClick={onAttachAnother}
        className="w-full flex items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/25 p-3 text-sm text-muted-foreground hover:border-primary/50 hover:text-primary transition-colors"
      >
        <Paperclip className="h-4 w-4" />
        Attach Another Receipt
      </button>

      {/* Undo toast */}
      {undoState && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-60 flex items-center gap-3 rounded-lg bg-foreground px-4 py-3 text-sm text-background shadow-lg">
          <span>Receipt removed: {undoState.filename}</span>
          <button
            onClick={handleUndo}
            disabled={addProof.isPending}
            className="flex items-center gap-1 font-medium text-primary hover:underline disabled:opacity-50"
          >
            {addProof.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Undo2 className="h-3.5 w-3.5" />
            )}
            {addProof.isPending ? "Restoring..." : "Undo"}
          </button>
        </div>
      )}
    </div>
  );
}
