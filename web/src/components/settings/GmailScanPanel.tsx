import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  CalendarIcon,
  CheckCircle,
  Loader2,
  Search,
  Download,
  FileCheck,
  Archive,
  AlertCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useGmailScan, type ScanRun } from "@/hooks/use-gmail-scan";

function formatDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function formatDisplayDate(d: Date): string {
  return d.toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

type DatePreset = "30d" | "3m" | "year" | "custom";

const PHASE_STEPS = ["search", "download", "score", "extract", "store"] as const;
const PHASE_LABELS: Record<string, string> = {
  search: "Search",
  download: "Download",
  score: "Score",
  extract: "Extract",
  store: "Store",
};

function ScanProgressIndicator({
  phase,
  detail,
}: {
  phase: string;
  detail: string;
}) {
  const currentIndex = PHASE_STEPS.indexOf(phase as typeof PHASE_STEPS[number]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {PHASE_STEPS.map((step, i) => {
          const isDone = i < currentIndex;
          const isActive = i === currentIndex;
          return (
            <div key={step} className="flex items-center gap-1.5">
              <div
                className={cn(
                  "flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-medium",
                  isDone && "bg-primary text-primary-foreground",
                  isActive && "bg-primary/20 text-primary ring-2 ring-primary/40",
                  !isDone && !isActive && "bg-muted text-muted-foreground"
                )}
              >
                {isDone ? (
                  <CheckCircle className="h-3 w-3" />
                ) : (
                  PHASE_LABELS[step]?.[0]
                )}
              </div>
              <span
                className={cn(
                  "text-xs hidden sm:inline",
                  isActive ? "text-primary font-medium" : "text-muted-foreground"
                )}
              >
                {PHASE_LABELS[step]}
              </span>
              {i < PHASE_STEPS.length - 1 && (
                <div
                  className={cn(
                    "h-px w-3",
                    isDone ? "bg-primary" : "bg-border"
                  )}
                />
              )}
            </div>
          );
        })}
      </div>
      <p className="text-sm text-muted-foreground">{detail}</p>
    </div>
  );
}

function ScanResults({ scan }: { scan: ScanRun }) {
  if (scan.status === "FAILED") {
    return (
      <div className="rounded-lg border border-rose-200 bg-rose-50 p-3 space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium text-rose-700">
          <AlertCircle className="h-4 w-4" />
          Scan failed
        </div>
        {scan.error_message && (
          <p className="text-xs text-rose-600">{scan.error_message}</p>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-primary/20 bg-primary/5 p-3 space-y-1.5">
      <div className="flex items-center gap-2 text-sm font-medium text-primary">
        <CheckCircle className="h-4 w-4" />
        Scan complete
        {scan.completed_at && (
          <span className="text-xs font-normal text-muted-foreground">
            {new Date(scan.completed_at).toLocaleDateString("en-CA", {
              month: "short",
              day: "numeric",
              year: "numeric",
            })}
          </span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <div className="flex items-center gap-1.5">
          <Search className="h-3 w-3" />
          {scan.emails_scanned} emails scanned
        </div>
        <div className="flex items-center gap-1.5">
          <Download className="h-3 w-3" />
          {scan.documents_found} documents found
        </div>
        <div className="flex items-center gap-1.5">
          <FileCheck className="h-3 w-3" />
          {scan.documents_added} new receipts added
        </div>
        <div className="flex items-center gap-1.5">
          <Archive className="h-3 w-3" />
          {scan.documents_skipped} skipped
        </div>
      </div>
    </div>
  );
}

export function GmailScanPanel() {
  const {
    activeScan,
    isScanning,
    scanProgress,
    startScan,
  } = useGmailScan();

  const [preset, setPreset] = useState<DatePreset>("30d");
  const [customFrom, setCustomFrom] = useState<Date | undefined>();
  const [customTo, setCustomTo] = useState<Date | undefined>();
  const [expanded, setExpanded] = useState(false);

  function getDateRange(): { since_date?: string; until_date?: string } {
    const now = new Date();
    switch (preset) {
      case "30d":
        return {
          since_date: formatDate(
            new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000)
          ),
        };
      case "3m":
        return {
          since_date: formatDate(
            new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000)
          ),
        };
      case "year": {
        const jan1 = new Date(now.getFullYear(), 0, 1);
        return { since_date: formatDate(jan1) };
      }
      case "custom":
        return {
          ...(customFrom ? { since_date: formatDate(customFrom) } : {}),
          ...(customTo ? { until_date: formatDate(customTo) } : {}),
        };
    }
  }

  function handleStartScan() {
    startScan.mutate(getDateRange());
  }

  // Show results from last completed scan if not scanning
  const lastCompletedScan =
    !isScanning && activeScan && activeScan.status !== "RUNNING"
      ? activeScan
      : null;

  // Show results inline after a scan completes
  if (lastCompletedScan && !expanded) {
    return (
      <div className="space-y-3 pt-2">
        <ScanResults scan={lastCompletedScan} />
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setExpanded(true)}
            className="text-primary border-primary/20 hover:bg-primary/5"
          >
            Scan again
          </Button>
        </div>
      </div>
    );
  }

  // Scanning in progress
  if (isScanning) {
    return (
      <div className="space-y-3 pt-2">
        <div className="flex items-center gap-2 text-sm text-primary">
          <Loader2 className="h-4 w-4 animate-spin" />
          Scanning...
        </div>
        {scanProgress && (
          <ScanProgressIndicator
            phase={scanProgress.phase}
            detail={scanProgress.detail}
          />
        )}
      </div>
    );
  }

  // Scan panel (expanded or first time)
  return (
    <div className="space-y-3 pt-2">
      <p className="text-sm text-muted-foreground">Scan period:</p>
      <div className="flex flex-wrap gap-2">
        {(
          [
            ["30d", "Last 30 days"],
            ["3m", "Last 3 months"],
            ["year", "This year"],
            ["custom", "Custom"],
          ] as const
        ).map(([value, label]) => (
          <Button
            key={value}
            variant={preset === value ? "default" : "outline"}
            size="sm"
            onClick={() => setPreset(value)}
            className={cn(
              preset === value
                ? "bg-primary hover:bg-primary/90"
                : "border-primary/20 text-primary hover:bg-primary/5"
            )}
          >
            {label}
          </Button>
        ))}
      </div>

      {preset === "custom" && (
        <div className="flex items-center gap-2">
          <Popover>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="w-[140px] justify-start text-left font-normal"
              >
                <CalendarIcon className="mr-2 h-4 w-4" />
                {customFrom ? formatDisplayDate(customFrom) : "From date"}
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-auto p-0" align="start">
              <Calendar
                mode="single"
                selected={customFrom}
                onSelect={setCustomFrom}
                disabled={(date) => date > new Date()}
              />
            </PopoverContent>
          </Popover>
          <span className="text-muted-foreground">to</span>
          <Popover>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="w-[140px] justify-start text-left font-normal"
              >
                <CalendarIcon className="mr-2 h-4 w-4" />
                {customTo ? formatDisplayDate(customTo) : "To date"}
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-auto p-0" align="start">
              <Calendar
                mode="single"
                selected={customTo}
                onSelect={setCustomTo}
                disabled={(date) => date > new Date()}
              />
            </PopoverContent>
          </Popover>
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          onClick={handleStartScan}
          disabled={startScan.isPending}
          className="bg-primary hover:bg-primary/90"
        >
          {startScan.isPending && (
            <Loader2 className="h-4 w-4 mr-1 animate-spin" />
          )}
          Start Scan
        </Button>
        {expanded && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExpanded(false)}
            className="text-muted-foreground"
          >
            Cancel
          </Button>
        )}
      </div>

      {startScan.isError && (
        <p className="text-xs text-rose-600">
          {(startScan.error as Error)?.message || "Failed to start scan"}
        </p>
      )}
    </div>
  );
}
