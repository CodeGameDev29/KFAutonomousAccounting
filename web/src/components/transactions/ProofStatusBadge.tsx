import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface ProofStatusBadgeProps {
  /** Receipt id, for the "not linked yet" message. */
  docId: number;
  /** Document status: MATCHED | EXTRACTED | … */
  status: string;
  /** First transaction this receipt proves, or null when it proves none. */
  linkedTxnId: number | null;
  /** Total transactions covered — >1 means split coverage (ONE_TO_MANY). */
  linkedTxnCount?: number;
  /** source_file of the linked transaction, for retargeting the statement tab. */
  linkedSourceFile: string | null;
  onJump: (txnId: number, sourceFile: string | null) => void;
}

function statusLabel(status: string): string {
  if (status === "MATCHED") return "Matched";
  if (status === "EXTRACTED") return "Ready";
  return status;
}

/**
 * Status badge on a receipt/invoice card, clickable whenever the receipt is
 * linked to a transaction.
 *
 * The clickable jump is gated on the LINK, never on the status: a receipt can
 * prove a transaction while its own status still reads "Ready" (e.g. a match
 * that is still awaiting review), and such a receipt must still be able to
 * point at the transaction it belongs to.
 */
export function ProofStatusBadge({
  docId,
  status,
  linkedTxnId,
  linkedTxnCount,
  linkedSourceFile,
  onJump,
}: ProofStatusBadgeProps) {
  const label = statusLabel(status);
  const isMatched = status === "MATCHED";
  const colors = isMatched
    ? "bg-primary/10 text-primary border-primary/20"
    : "bg-primary/5 text-primary/60 border-primary/10";

  // Unlinked: still clickable, but it says so rather than dead-clicking — the
  // whole point of the badge is to answer "which transaction is this on?".
  if (linkedTxnId == null) {
    return (
      <button
        type="button"
        className="shrink-0"
        title="Not linked to a transaction yet"
        aria-label={`Receipt #${docId} is not linked to a transaction yet`}
        onClick={(e) => {
          e.stopPropagation();
          toast.info(`Receipt #${docId} isn't linked to a transaction yet.`, {
            description:
              "Reconcile, or open a transaction and attach it as proof, to link it.",
          });
        }}
      >
        <Badge className={cn("text-[10px] shrink-0 cursor-pointer", colors)}>
          {label}
        </Badge>
      </button>
    );
  }

  const count = linkedTxnCount ?? 1;

  return (
    <button
      type="button"
      className="shrink-0"
      title={
        count > 1
          ? `Click to jump to the first of ${count} linked transactions`
          : "Click to jump to the linked transaction"
      }
      aria-label="Jump to the transaction this receipt is linked to"
      onClick={(e) => {
        e.stopPropagation();
        // For split coverage, jump to the first transaction; the blinking
        // highlight on arrival makes the landing row unambiguous.
        onJump(linkedTxnId, linkedSourceFile);
      }}
    >
      <Badge
        className={cn(
          "text-[10px] shrink-0 cursor-pointer transition-colors hover:bg-primary/20",
          colors,
        )}
      >
        {label}
        {count > 1 ? ` (${count})` : ""}
        {" ↗"}
      </Badge>
    </button>
  );
}
