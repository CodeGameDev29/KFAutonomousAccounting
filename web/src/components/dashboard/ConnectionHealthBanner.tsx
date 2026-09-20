import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ConnectionInfo {
  source: string;
  enabled: boolean;
  connected: boolean;
  status: string;
  last_checked: string | null;
  last_success: string | null;
  error_message: string | null;
}

interface ConnectionsHealthResponse {
  connections: Record<string, ConnectionInfo>;
}

const SOURCE_LABELS: Record<string, string> = {
  gmail: "Gmail",
  wise: "Wise",
};

export function ConnectionHealthBanner() {
  const [dismissed, setDismissed] = useState(false);

  const { data } = useQuery<ConnectionsHealthResponse>({
    queryKey: ["connections", "health"],
    queryFn: () => api.get("/api/connections/health"),
    // Only poll every 5 minutes — connection health doesn't change rapidly
    refetchInterval: 5 * 60 * 1000,
  });

  if (dismissed || !data) {
    return null;
  }

  // Find connections that are enabled but have issues
  const problemSources = Object.values(data.connections).filter(
    (c) =>
      c.enabled &&
      (c.status === "ERROR" ||
        c.status === "DEGRADED" ||
        c.status === "DISCONNECTED" ||
        (!c.connected && c.status !== "HEALTHY"))
  );

  if (problemSources.length === 0) {
    return null;
  }

  const sourceNames = problemSources
    .map((s) => SOURCE_LABELS[s.source] ?? s.source)
    .join(", ");

  // Determine severity: rose for ERROR/DISCONNECTED, amber for DEGRADED
  const hasError = problemSources.some(
    (s) => s.status === "ERROR" || s.status === "DISCONNECTED"
  );
  const containerClass = hasError
    ? "border-rose-200 bg-rose-50 text-rose-800"
    : "border-amber-200 bg-amber-50 text-amber-800";
  const iconClass = hasError ? "text-rose-600" : "text-amber-600";
  const linkClass = hasError
    ? "text-rose-800 hover:text-rose-900"
    : "text-amber-800 hover:text-amber-900";
  const dismissClass = hasError
    ? "text-rose-600 hover:bg-rose-100 hover:text-rose-800"
    : "text-amber-600 hover:bg-amber-100 hover:text-amber-800";

  return (
    <div className={`flex items-center justify-between rounded-xl border px-4 py-3 shadow-warm-sm ${containerClass}`}>
      <div className="flex items-center gap-3">
        <AlertTriangle className={`h-5 w-5 shrink-0 ${iconClass}`} />
        <div className="text-sm">
          <span className="font-medium">
            Connection issue
          </span>
          <span>
            {" "}&mdash; {sourceNames}{" "}
            {problemSources.length === 1
              ? "needs attention"
              : "need attention"}
            .{" "}
          </span>
          <Link
            to="/settings?tab=connections"
            className={`font-medium underline underline-offset-2 ${linkClass}`}
          >
            View Settings
          </Link>
        </div>
      </div>
      <Button
        variant="ghost"
        size="sm"
        className={`h-7 w-7 shrink-0 p-0 ${dismissClass}`}
        onClick={() => setDismissed(true)}
        aria-label="Dismiss"
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  );
}
