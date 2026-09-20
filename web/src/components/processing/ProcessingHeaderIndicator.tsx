/**
 * The compact "Processing 3 / 44" chip in the app header.
 *
 * It lives in the header's normal flow, left-aligned, for the reason recorded
 * in AppLayout: a floating corner control sits on top of pagination rows,
 * slide-over footers and dialog action rows and takes those clicks. Nothing can
 * end up underneath a chip that is part of the layout.
 *
 * Visible on every page while anything is in flight, so a user who wanders off
 * to Reports mid-run still knows the machine is working, and one click brings
 * the full list back.
 */
import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import {
  useProcessingQueue,
  useDisplayCounts,
  useEtaSeconds,
  useFreshness,
  selectInFlight,
} from "@/stores/processing-queue-store";
import { formatEta, formatLastKnown } from "@/lib/processing-queue-display";
import { cn } from "@/lib/utils";
import { Loader2, AlertTriangle, CloudOff } from "lucide-react";

export function ProcessingHeaderIndicator({ className }: { className?: string }) {
  const navigate = useNavigate();
  const inFlight = useProcessingQueue(selectInFlight);
  const counts = useDisplayCounts();
  const etaSeconds = useEtaSeconds();
  const modelReachable = useProcessingQueue((s) => s.worker.model_reachable);
  const { stale, lastSyncAt } = useFreshness();

  // Only to keep the age in the tooltip counting up while the server is quiet.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!stale) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [stale]);

  if (!inFlight) return null;

  const working = counts.uploading + counts.running;
  const total = counts.total;
  // Same rule as the panel: a stale estimate is worse than no estimate.
  const eta = stale ? null : formatEta(etaSeconds);
  const modelDown = !modelReachable;
  const lastKnown = stale && lastSyncAt ? formatLastKnown(now - lastSyncAt) : null;

  const detail = [
    `${counts.queued} queued`,
    counts.waiting_for_model > 0 ? `${counts.waiting_for_model} waiting for the model` : null,
    `${counts.done} done`,
    counts.failed > 0 ? `${counts.failed} failed` : null,
    eta,
    lastKnown,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <button
      type="button"
      onClick={() => navigate("/transactions?queue=1#processing-queue")}
      title={`Open the processing list — ${detail}`}
      aria-label={`Processing ${working} of ${total} documents. ${detail}. Open the processing list.`}
      className={cn(
        "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
        stale
          ? "border-border/60 bg-muted/40 text-muted-foreground hover:bg-muted/60"
          : modelDown
            ? "border-warning/30 bg-warning/[0.08] text-warning hover:bg-warning/[0.14]"
            : "border-primary/25 bg-primary/[0.06] text-primary hover:bg-primary/[0.12]",
        className,
      )}
    >
      {stale ? (
        // No spinner while nothing is confirming that anything is spinning.
        <CloudOff className="h-3.5 w-3.5" aria-hidden="true" />
      ) : modelDown ? (
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
      ) : (
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
      )}
      <span className="tabular-nums">
        Processing {working} / {total}
      </span>
      {stale && <span className="hidden sm:inline">&middot; {lastKnown}</span>}
      {!stale && modelDown && <span className="hidden sm:inline">&middot; model offline</span>}
    </button>
  );
}
