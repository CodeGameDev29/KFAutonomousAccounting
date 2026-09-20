import { useEffect, useRef } from "react";
import { AlertTriangle, CheckCircle, Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export type ProgressLogPanelStatus = "idle" | "running" | "complete" | "error";

export interface ProgressLogPanelProps {
  status: ProgressLogPanelStatus;
  phase?: string;
  detail?: string;
  percent?: number;
  logs: string[];
  rightBadge?: React.ReactNode;
  onClose?: () => void;
  className?: string;
  maxHeightClass?: string;
}

/**
 * Presentation-only log window used by reconciliation progress banner and
 * the categorization agent modal. Handles auto-scroll, status icon, progress
 * bar, and bounded log list.
 */
export function ProgressLogPanel({
  status,
  phase,
  detail,
  percent,
  logs,
  rightBadge,
  onClose,
  className,
  maxHeightClass = "max-h-48",
}: ProgressLogPanelProps) {
  const logContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [logs]);

  const isBusy = status === "running";

  return (
    <div
      className={cn(
        "rounded-xl border p-4 shadow-warm-sm",
        status === "error"
          ? "border-destructive/20 bg-destructive/5"
          : "border-primary/20 bg-primary/5",
        className,
      )}
    >
      <div className="flex items-center gap-3 mb-3">
        <div
          className={cn(
            "flex-shrink-0 h-8 w-8 rounded-full flex items-center justify-center",
            status === "error" ? "bg-destructive/10" : "bg-primary/10",
          )}
        >
          {isBusy ? (
            <Loader2 className="h-4 w-4 text-primary animate-spin" />
          ) : status === "error" ? (
            <AlertTriangle className="h-4 w-4 text-destructive" />
          ) : (
            <CheckCircle className="h-4 w-4 text-primary" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <p
            className={cn(
              "text-sm font-medium",
              status === "error" ? "text-destructive" : "text-primary",
            )}
          >
            {phase || (isBusy ? "Working..." : status === "complete" ? "Complete" : "Idle")}
          </p>
          {detail && (
            <p className="text-xs text-primary/70 mt-0.5 truncate">{detail}</p>
          )}
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {rightBadge}
          {isBusy && (
            <span className="text-xs font-mono text-primary/60 tabular-nums">
              {percent ?? 0}%
            </span>
          )}
          {!isBusy && onClose && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 rounded-full hover:bg-muted"
              onClick={onClose}
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>
      {isBusy && (
        <div className="h-2.5 bg-primary/10 rounded-full overflow-hidden">
          <div
            className="h-full bg-primary rounded-full transition-all duration-700 ease-out"
            style={{ width: `${Math.max(percent ?? 0, 2)}%` }}
          />
        </div>
      )}
      {logs.length > 0 && (
        <div
          ref={logContainerRef}
          className={cn(
            "overflow-y-auto rounded-lg bg-background/80 border p-2 font-mono text-[11px] leading-relaxed",
            maxHeightClass,
            isBusy ? "mt-3" : "mt-2",
            status === "error"
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
  );
}
