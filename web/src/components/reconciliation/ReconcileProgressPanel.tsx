/**
 * The reconciliation progress banner: phase, percent, matches-so-far, and a
 * collapsible log.
 *
 * `/api/reconciliation/progress` returns rich detail while a run is in flight:
 * the phase ("Cross-statement linking..."), a percent, matches so far and a
 * running log. Both the Transactions page and the Review page start that same
 * job, so the rendering lives here — otherwise one of them would show the
 * detail and the other only "Matching...", for however long the run takes.
 */

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertTriangle, CheckCircle, Loader2, X } from "lucide-react";
import type { ReconcileRunResult } from "@/lib/reconciliation-result";

export interface ReconcileProgressState {
  status: "idle" | "running" | "complete" | "error";
  phase?: string;
  detail?: string;
  percent?: number;
  /** In flight: pairs the engine has proposed. At the end: matches written. */
  matches_so_far?: number;
  /** Present once the run is complete — the full run summary. */
  result?: ReconcileRunResult;
  logs?: string[];
  /** Units of work in the phase now running (documents, then candidate pairs). */
  pairs_total?: number;
  /** Units finished in that phase. */
  pairs_done?: number;
  /** Engine LLM calls made so far this run. */
  llm_calls_done?: number;
  /** Rolling estimate for the phase now running; null until it is trustworthy. */
  eta_seconds?: number | null;
}

/**
 * "about 4 min left" / "about 40s left". Deliberately coarse: the estimate is a
 * rolling average over the last few completions, so a to-the-second countdown
 * would claim a precision it does not have.
 */
function formatEta(seconds: number): string {
  if (seconds < 45) return `about ${Math.max(5, Math.round(seconds / 5) * 5)}s left`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `about ${hours}h ${rest}m left` : `about ${hours}h left`;
}

interface Props {
  progress: ReconcileProgressState | null;
  /** Client-side run log, newest last. */
  logs: string[];
  /** True while caches are being refetched after the run finished. */
  saving?: boolean;
  /** Omit to make the banner non-dismissable. */
  onDismiss?: () => void;
  className?: string;
}

export function ReconcileProgressPanel({
  progress,
  logs,
  saving = false,
  onDismiss,
  className,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);

  // Keep the log window pinned to the newest line while it grows.
  useEffect(() => {
    if (expanded && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logs, expanded]);

  if (!progress || logs.length === 0) return null;

  const isRunning = progress.status === "running";
  const isBusy = isRunning || saving;

  return (
    <div
      data-testid="reconcile-progress"
      className={cn(
        "rounded-xl border p-4 shadow-warm-sm",
        progress.status === "error"
          ? "border-destructive/20 bg-destructive/5"
          : "border-primary/20 bg-primary/5",
        className,
      )}
    >
      <div className="flex items-center gap-3 mb-3">
        <div
          className={cn(
            "flex-shrink-0 h-8 w-8 rounded-full flex items-center justify-center",
            progress.status === "error" ? "bg-destructive/10" : "bg-primary/10",
          )}
        >
          {isBusy ? (
            <Loader2 className="h-4 w-4 text-primary animate-spin" />
          ) : progress.status === "error" ? (
            <AlertTriangle className="h-4 w-4 text-destructive" />
          ) : (
            <CheckCircle className="h-4 w-4 text-primary" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <p
            className={cn(
              "text-sm font-medium",
              progress.status === "error" ? "text-destructive" : "text-primary",
            )}
          >
            {saving
              ? "Saving Results..."
              : progress.status === "complete"
                ? "Matching Complete"
                : progress.status === "error"
                  ? "Matching Failed"
                  : progress.phase || "Matching..."}
          </p>
          {saving && (
            <p className="text-xs text-primary/70 mt-0.5">
              Syncing all data before the queue refreshes...
            </p>
          )}
          {isRunning && progress.detail && (
            <p className="text-xs text-primary/70 mt-0.5">{progress.detail}</p>
          )}
          {isRunning && (progress.pairs_total ?? 0) > 0 && (
            <p
              data-testid="reconcile-eta"
              className="text-xs text-primary/70 mt-0.5 tabular-nums"
            >
              {progress.pairs_done ?? 0} of {progress.pairs_total} done
              {/* Only claim a countdown while there is something left to count.
                  Between two long phases the counts read "n of n", and an ETA
                  there would be a number about nothing. */}
              {(progress.pairs_done ?? 0) < (progress.pairs_total ?? 0) &&
                (typeof progress.eta_seconds === "number"
                  ? ` — ${formatEta(progress.eta_seconds)}`
                  : " — estimating time left...")}
              {(progress.llm_calls_done ?? 0) > 0 &&
                ` · ${progress.llm_calls_done} AI call${progress.llm_calls_done === 1 ? "" : "s"}`}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {(progress.matches_so_far ?? 0) > 0 && (
            <Badge className="bg-primary/10 text-primary border-primary/20 font-mono text-xs">
              {progress.status === "complete"
                ? `${progress.matches_so_far} new match${progress.matches_so_far === 1 ? "" : "es"}`
                : `${progress.matches_so_far} matches`}
            </Badge>
          )}
          {isBusy && (
            <span className="text-xs font-mono text-primary/60 tabular-nums">
              {saving ? "syncing" : `${progress.percent ?? 0}%`}
            </span>
          )}
          {!isBusy && onDismiss && (
            <Button
              variant="ghost"
              size="icon"
              aria-label="Dismiss matching progress"
              className="h-6 w-6 rounded-full hover:bg-muted"
              onClick={onDismiss}
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>

      {isBusy && (
        <div className="h-2.5 bg-primary/10 rounded-full overflow-hidden">
          <div
            className={cn(
              "h-full bg-primary rounded-full transition-all duration-700 ease-out",
              saving && "animate-pulse",
            )}
            style={{
              width: saving ? "100%" : `${Math.max(progress.percent ?? 0, 2)}%`,
            }}
          />
        </div>
      )}

      <div className={isBusy ? "mt-3" : "mt-2"}>
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1 mb-1"
        >
          {expanded ? "Hide details" : "Show details"}
          <span className="text-[10px]">{expanded ? "▲" : "▼"}</span>
        </button>
        {expanded && (
          <div
            ref={logRef}
            className={cn(
              "max-h-48 overflow-y-auto rounded-lg bg-background/80 border p-2 font-mono text-[11px] leading-relaxed",
              progress.status === "error"
                ? "border-destructive/10 text-muted-foreground"
                : "border-primary/10 text-primary/70",
            )}
          >
            {logs.map((log, i) => (
              <div
                key={i}
                className={cn(
                  "py-0.5",
                  log.startsWith("ERROR") && "text-destructive font-semibold",
                  log.startsWith("  ") && "pl-4 text-muted-foreground/70",
                )}
              >
                <span className="text-primary/30 mr-2 select-none">
                  {String(i + 1).padStart(2, "0")}
                </span>
                {log}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
