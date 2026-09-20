/**
 * ProcessingQueuePanel — what is happening to the files that were just dropped.
 *
 * Sits on the Transactions upload area and stays visible whether or not an
 * upload zone is open, because the interesting part of a bulk drop happens long
 * after the drop zone has served its purpose.
 *
 * Three things it must never do: go quiet, imply a file was lost, or show a
 * spinner with no sentence next to it.
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  useProcessingQueue,
  useQueueRows,
  useDisplayCounts,
  useBatchProgress,
  useEtaSeconds,
  useFreshness,
  ACTIVE_STATUSES,
  TERMINAL_STATUSES,
  CLEARABLE_FINISHED,
  CLEARABLE_FAILED,
  type QueueRow,
} from "@/stores/processing-queue-store";
import {
  STATUS_LABELS,
  stageText,
  viewHrefFor,
  kindLabel,
  formatElapsed,
  formatLastKnown,
  elapsedMsFor,
  summaryStripText,
} from "@/lib/processing-queue-display";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Loader2,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Clock,
  PauseCircle,
  Copy,
  Upload,
  FileSearch,
  RotateCcw,
  ArrowUpRight,
  X,
  ChevronLeft,
  ChevronRight,
  WifiOff,
  CloudOff,
} from "lucide-react";

const PAGE_SIZE = 12;

interface ProcessingQueuePanelProps {
  /** Render even with an empty queue (used when the header indicator sends
   *  the user here and there is nothing left to see). */
  alwaysShow?: boolean;
  className?: string;
  id?: string;
}

/** Icon per state. Shape carries the meaning; colour only reinforces it. */
function StatusIcon({ status }: { status: QueueRow["status"] }) {
  const base = "h-4 w-4";
  switch (status) {
    case "uploading":
      return <Upload className={cn(base, "text-primary")} aria-hidden="true" />;
    case "queued":
      return <Clock className={cn(base, "text-muted-foreground")} aria-hidden="true" />;
    case "running":
      return <Loader2 className={cn(base, "animate-spin text-primary")} aria-hidden="true" />;
    case "waiting_for_model":
      return <PauseCircle className={cn(base, "text-warning")} aria-hidden="true" />;
    case "done":
      return <CheckCircle2 className={cn(base, "text-success")} aria-hidden="true" />;
    case "failed":
    case "upload_failed":
      return <XCircle className={cn(base, "text-destructive")} aria-hidden="true" />;
    case "cancelled":
      return <X className={cn(base, "text-muted-foreground")} aria-hidden="true" />;
    case "duplicate":
      return <Copy className={cn(base, "text-warning")} aria-hidden="true" />;
    default:
      return null;
  }
}

/** Text pill. Never colour on its own — the word is always there. */
function StatusPill({ status }: { status: QueueRow["status"] }) {
  const tone =
    status === "done"
      ? "bg-success/10 text-success border-success/20"
      : status === "failed" || status === "upload_failed"
        ? "bg-destructive/10 text-destructive border-destructive/20"
        : status === "waiting_for_model" || status === "duplicate"
          ? "bg-warning/10 text-warning border-warning/25"
          : status === "cancelled"
            ? "bg-muted text-muted-foreground border-border"
            : "bg-primary/10 text-primary border-primary/20";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-none",
        tone,
      )}
    >
      {STATUS_LABELS[status]}
    </span>
  );
}

function QueueRowItem({
  row,
  now,
  busy,
  onRetry,
  onCancel,
  onDismiss,
  onReroute,
}: {
  row: QueueRow;
  now: number;
  busy: boolean;
  onRetry: (id: string) => void;
  onCancel: (id: string) => void;
  onDismiss: (id: string) => void;
  onReroute: (id: string) => void;
}) {
  const elapsed = elapsedMsFor(row, now);
  // A waiting row normally says "Waiting for the extraction model", which is
  // true but motionless. When the server has something more specific to say
  // about the wait — "Attempt 2 of 3 timed out; retrying" — that
  // sentence is the one that shows a person the job is still climbing rather
  // than parked. Read defensively: an older backend sends nothing here.
  const waitingDetail =
    row.status === "waiting_for_model" ? (row.error_message ?? "").trim() : "";
  const stage = waitingDetail || stageText(row);
  const href = row.status === "done" || row.status === "duplicate" ? viewHrefFor(row) : null;
  const isActive = ACTIVE_STATUSES.has(row.status);
  const isTerminal = TERMINAL_STATUSES.has(row.status);
  const canRetry =
    row.status === "failed" ||
    row.status === "cancelled" ||
    (row.status === "upload_failed" && !!row.file);
  // A page bar only means something when pages actually land one batch at a
  // time. For a document that goes up in a single call there is nothing to
  // fill, and a bar frozen at one width for the length of the call reads as a
  // stall — so the bar waits for the engine to say it is reading in batches.
  // In the second before
  // it says anything, no bar at all beats an empty one.
  const pagePercent =
    row.result?.mode === "paged" && row.pages_total && row.pages_total > 0
      ? Math.round(((row.pages_done ?? 0) / row.pages_total) * 100)
      : null;
  const detectedStatement =
    row.result?.detected_as === "statement" && row.kind === "statement";

  return (
    <li
      className={cn(
        "rounded-lg border px-3 py-2.5",
        row.status === "done"
          ? "border-border/50 bg-card"
          : row.status === "failed" || row.status === "upload_failed"
            ? "border-destructive/20 bg-destructive/[0.02]"
            : row.status === "waiting_for_model"
              ? "border-warning/25 bg-warning/[0.03]"
              : isActive
                ? "border-primary/25 bg-primary/[0.02]"
                : "border-border/50 bg-card",
      )}
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 shrink-0">
          <StatusIcon status={row.status} />
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <p className="truncate text-sm font-medium text-foreground" title={row.filename}>
              {row.filename}
            </p>
            <span className="shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground/70">
              {kindLabel(row.kind)}
            </span>
          </div>

          <p
            className={cn(
              "mt-0.5 text-xs",
              row.status === "failed" || row.status === "upload_failed"
                ? "text-destructive"
                : row.status === "waiting_for_model" || row.status === "duplicate"
                  ? "text-warning"
                  : row.status === "done"
                    ? "text-muted-foreground"
                    : "text-primary",
            )}
          >
            {stage}
            {elapsed != null && (isActive || row.status === "done") && (
              <span className="ml-1.5 font-mono tabular-nums text-muted-foreground/70">
                {row.status === "running" ? `\u00b7 elapsed ${formatElapsed(elapsed)}` : formatElapsed(elapsed)}
              </span>
            )}
            {(row.attempts ?? 0) > 1 && (
              <span className="ml-1.5 text-muted-foreground/70">
                &middot; attempt {row.attempts}
              </span>
            )}
          </p>

          {detectedStatement && (
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
              <span className="inline-flex items-center gap-1 rounded-full border border-primary/20 bg-primary/[0.06] px-1.5 py-0.5 font-medium text-primary">
                <FileSearch className="h-3 w-3" aria-hidden="true" />
                Detected as a bank statement
              </span>
              {!isTerminal && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onReroute(row.id)}
                  className="underline underline-offset-2 hover:text-foreground disabled:opacity-50"
                >
                  No, it&rsquo;s a receipt
                </button>
              )}
            </p>
          )}

          {pagePercent != null && row.status === "running" && (
            <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-primary/10">
              <div
                className="h-full rounded-full bg-primary transition-all duration-500 ease-out"
                style={{ width: `${Math.max(pagePercent, 3)}%` }}
              />
            </div>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <StatusPill status={row.status} />

          {href && (
            <Button
              asChild
              variant="ghost"
              size="sm"
              className="h-7 gap-1 px-2 text-xs text-primary hover:bg-primary/5"
            >
              <Link to={href}>
                View
                <ArrowUpRight className="h-3 w-3" />
              </Link>
            </Button>
          )}

          {canRetry && (
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => onRetry(row.id)}
              className="h-7 gap-1 px-2 text-xs text-primary hover:bg-primary/5"
            >
              <RotateCcw className="h-3 w-3" />
              Retry
            </Button>
          )}

          {isActive && (
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => onCancel(row.id)}
              className="h-7 gap-1 px-2 text-xs text-muted-foreground hover:bg-muted"
            >
              Cancel
            </Button>
          )}

          {isTerminal && (
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Remove ${row.filename} from this list`}
              onClick={() => onDismiss(row.id)}
              className="h-7 w-7 rounded-full text-muted-foreground hover:bg-muted"
            >
              <X className="h-3 w-3" />
            </Button>
          )}
        </div>
      </div>
    </li>
  );
}

export function ProcessingQueuePanel({
  alwaysShow = false,
  className,
  id,
}: ProcessingQueuePanelProps) {
  const rows = useQueueRows();
  const counts = useDisplayCounts();
  const progress = useBatchProgress();
  const etaSeconds = useEtaSeconds();
  const worker = useProcessingQueue((s) => s.worker);
  const connection = useProcessingQueue((s) => s.connection);
  const jobsApiAvailable = useProcessingQueue((s) => s.jobsApiAvailable);
  const syncError = useProcessingQueue((s) => s.syncError);
  const busyIds = useProcessingQueue((s) => s.busyIds);
  const { stale, lastSyncAt } = useFreshness();
  const retry = useProcessingQueue((s) => s.retry);
  const cancel = useProcessingQueue((s) => s.cancel);
  const reroute = useProcessingQueue((s) => s.rerouteToReceipt);
  const clearFinished = useProcessingQueue((s) => s.clearFinished);
  const clearFailed = useProcessingQueue((s) => s.clearFailed);
  const dismissRow = useProcessingQueue((s) => s.dismissRow);

  const [page, setPage] = useState(0);

  // One tick a second while something is moving — the elapsed clocks have to
  // advance or a long extraction reads as a frozen row — and also while the
  // picture is stale, because then it is the *age* of the numbers that has to
  // keep counting up.
  const hasActive = counts.active > 0;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!hasActive && !stale) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [hasActive, stale]);

  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  useEffect(() => {
    if (page > pageCount - 1) setPage(0);
  }, [page, pageCount]);

  const visible = useMemo(
    () => rows.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE),
    [rows, page],
  );

  // Two counts, two buttons. One "Clear finished" covering both would drop a
  // failed row from the list and let the next poll put it straight back, since
  // the server is only ever asked to delete one status per call.
  const finishedCount = rows.filter((r) => CLEARABLE_FINISHED.has(r.status)).length;
  const failedCount = rows.filter((r) => CLEARABLE_FAILED.has(r.status)).length;
  // Nothing has confirmed these counts since `lastSyncAt`, so the estimate goes
  // (it was derived from them) and the strip says how old they are instead.
  const strip = summaryStripText(counts, stale ? null : etaSeconds);
  const lastKnown = stale && lastSyncAt ? formatLastKnown(now - lastSyncAt) : null;

  if (rows.length === 0 && !alwaysShow) return null;

  const waitingCount = counts.waiting_for_model + counts.queued + counts.running;
  // Only an explicit false is an outage — a backend that predates the field
  // normalizes to true in the store, so this never invents one.
  const workerAlive = worker.alive !== false;

  return (
    <section
      id={id}
      aria-label="Document processing"
      className={cn(
        "space-y-3 rounded-xl border border-border/60 bg-card p-5 shadow-warm",
        className,
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-foreground">Processing</h3>
          {connection === "polling" && (
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              <WifiOff className="h-3 w-3" aria-hidden="true" />
              live updates paused, refreshing every 3&nbsp;s
            </span>
          )}
          {!jobsApiAvailable && (
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              <CloudOff className="h-3 w-3" aria-hidden="true" />
              showing this tab&rsquo;s uploads only
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {finishedCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void clearFinished()}
              className="h-7 px-2 text-xs text-muted-foreground hover:bg-muted"
            >
              Clear finished ({finishedCount})
            </Button>
          )}
          {failedCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void clearFailed()}
              className="h-7 px-2 text-xs text-destructive hover:bg-destructive/5 hover:text-destructive"
            >
              Clear failed ({failedCount})
            </Button>
          )}
        </div>
      </div>

      {/* A retry, cancel or refresh that did not land. Saying nothing would
          leave a button that visibly did nothing, which is the same failure as
          a silent hang. */}
      {syncError && (
        <p
          role="status"
          className="rounded-lg border border-destructive/20 bg-destructive/[0.03] px-3 py-2 text-xs text-destructive"
        >
          {syncError} &mdash; the list keeps refreshing, so this will correct
          itself if it was a blip.
        </p>
      )}

      {/* The worker thread itself is gone. Unlike an unreachable model, this
          one does NOT resolve itself: uploads are still accepted and still
          queued, and nothing will ever pick them up. It outranks the model
          banner because restarting the stack is the only fix. `alive` is read
          defensively in the store — absent means alive, never an invented
          outage. */}
      {!workerAlive && (
        <div
          role="status"
          className="flex items-start gap-2.5 rounded-lg border border-destructive/30 bg-destructive/[0.05] px-3 py-2.5"
        >
          <AlertTriangle
            className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
            aria-hidden="true"
          />
          <p className="text-xs leading-relaxed text-foreground">
            The processing worker has stopped; uploads are accepted but nothing
            will run until the stack restarts
          </p>
        </div>
      )}

      {/* The model is down. Say so plainly, say nothing is lost, and stop. */}
      {workerAlive && !worker.model_reachable && (
        <div
          role="status"
          className="flex items-start gap-2.5 rounded-lg border border-warning/30 bg-warning/[0.06] px-3 py-2.5"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
          <p className="text-xs leading-relaxed text-foreground">
            <span className="font-semibold">
              The extraction model is not reachable.
            </span>{" "}
            {waitingCount} document{waitingCount === 1 ? "" : "s"}{" "}
            {waitingCount === 1 ? "is" : "are"} waiting and will resume
            automatically; nothing has been lost.
          </p>
        </div>
      )}

      {/* Summary strip — the one line that has to be true at every moment.
          When the server is not answering, "true" means saying that these are
          the last numbers it gave and when: a confident "4 running · about
          12 min left" during a restart is simply false. */}
      <div
        aria-live="polite"
        role="status"
        className={cn(
          "rounded-lg border px-3 py-2",
          stale ? "border-dashed border-border/60 bg-muted/20" : "border-border/50 bg-muted/30",
        )}
      >
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {stale ? (
            <CloudOff className="h-3.5 w-3.5 shrink-0 text-muted-foreground/60" aria-hidden="true" />
          ) : counts.active > 0 ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" aria-hidden="true" />
          ) : counts.failed > 0 ? (
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden="true" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" aria-hidden="true" />
          )}
          <p
            className={cn(
              "text-xs font-medium",
              stale ? "text-muted-foreground/60" : "text-foreground",
            )}
          >
            {strip || "Nothing is processing right now."}
          </p>
          {lastKnown && (
            <span className="inline-flex shrink-0 items-center rounded-full border border-border/60 bg-background px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-muted-foreground">
              {lastKnown}
            </span>
          )}
        </div>

        {progress.total > 0 && (
          <div className="mt-2 space-y-1">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-primary/10">
              <div
                className={cn(
                  "h-full rounded-full transition-all duration-500 ease-out",
                  stale
                    ? "bg-muted-foreground/30"
                    : progress.done === progress.total
                      ? "bg-success"
                      : "bg-primary",
                )}
                style={{ width: `${Math.max(progress.percent, 2)}%` }}
              />
            </div>
            <p
              className={cn(
                "font-mono text-[10px] tabular-nums",
                stale ? "text-muted-foreground/50" : "text-muted-foreground/80",
              )}
            >
              {progress.done} of {progress.total} finished in this batch
              {progress.total > 0 ? ` · ${progress.percent}%` : ""}
            </p>
          </div>
        )}
      </div>

      {rows.length === 0 ? (
        <p className="py-2 text-xs text-muted-foreground">
          Drop statements or receipts above and every file will show up here with
          its own progress.
        </p>
      ) : (
        <>
          <ul className="space-y-1.5">
            {visible.map((row) => (
              <QueueRowItem
                key={row.id}
                row={row}
                now={now}
                busy={busyIds.includes(row.id)}
                onRetry={(rid) => void retry(rid)}
                onCancel={(rid) => void cancel(rid)}
                onDismiss={dismissRow}
                onReroute={(rid) => void reroute(rid)}
              />
            ))}
          </ul>

          {pageCount > 1 && (
            <div className="flex items-center justify-between pt-1">
              <p className="text-[11px] text-muted-foreground">
                Showing {page * PAGE_SIZE + 1}&ndash;
                {Math.min(rows.length, (page + 1) * PAGE_SIZE)} of {rows.length}
              </p>
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2"
                  disabled={page === 0}
                  aria-label="Previous page of the processing list"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </Button>
                <span className="px-1 font-mono text-[11px] tabular-nums text-muted-foreground">
                  {page + 1}/{pageCount}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2"
                  disabled={page >= pageCount - 1}
                  aria-label="Next page of the processing list"
                  onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
