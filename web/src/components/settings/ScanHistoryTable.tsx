import { Badge } from "@/components/ui/badge";
import { useGmailScan, type ScanRun } from "@/hooks/use-gmail-scan";
import { cn } from "@/lib/utils";

function StatusBadge({ status }: { status: ScanRun["status"] }) {
  const config = {
    RUNNING: { label: "Running", className: "bg-blue-100 text-blue-700 border-blue-200" },
    COMPLETED: { label: "Completed", className: "bg-primary/10 text-primary border-primary/20" },
    FAILED: { label: "Failed", className: "bg-rose-100 text-rose-700 border-rose-200" },
    CANCELLED: { label: "Cancelled", className: "bg-gray-100 text-gray-600 border-gray-200" },
  }[status];

  return (
    <Badge variant="outline" className={cn("text-[10px] px-1.5 py-0", config.className)}>
      {config.label}
    </Badge>
  );
}

function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return "-";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

function formatTime(dateStr: string | null): string {
  if (!dateStr) return "-";
  return new Date(dateStr).toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function ScanHistoryTable() {
  const { scanHistory, historyLoading } = useGmailScan();

  if (historyLoading || scanHistory.length === 0) return null;

  return (
    <div className="pt-3 border-t">
      <p className="text-xs font-medium text-muted-foreground mb-2">
        Scan History
      </p>
      <div className="space-y-1.5">
        {scanHistory.slice(0, 5).map((run) => (
          <div
            key={run.id}
            className="flex items-center justify-between text-xs text-muted-foreground rounded-md px-2 py-1.5 hover:bg-muted/50"
          >
            <div className="flex items-center gap-2 min-w-0">
              <StatusBadge status={run.status} />
              <span className="truncate">{formatTime(run.started_at)}</span>
            </div>
            <div className="flex items-center gap-3 flex-shrink-0">
              <span>{run.emails_scanned} emails</span>
              <span>{run.documents_added} added</span>
              <span className="text-muted-foreground/60">
                {formatDuration(run.started_at, run.completed_at)}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
