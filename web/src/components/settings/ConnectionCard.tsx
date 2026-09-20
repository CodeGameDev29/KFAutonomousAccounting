import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Loader2, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

type HealthStatus = "HEALTHY" | "DEGRADED" | "ERROR" | "DISCONNECTED" | "UNKNOWN";

interface ConnectionCardProps {
  title: string;
  description: string;
  icon: ReactNode;
  connected: boolean;
  enabled: boolean;
  loading?: boolean;
  healthStatus?: HealthStatus;
  lastSync?: string | null;
  children?: ReactNode;
  onConnect?: () => void;
  onDisconnect?: () => void;
  onReconnect?: () => void;
}

function StatusDot({ status }: { status: HealthStatus }) {
  const colorClass = {
    HEALTHY: "bg-primary",
    DEGRADED: "bg-amber-500",
    ERROR: "bg-rose-500",
    DISCONNECTED: "bg-gray-400",
    UNKNOWN: "bg-gray-400",
  }[status];

  return (
    <span
      className={cn("inline-block h-2 w-2 rounded-full", colorClass)}
      aria-label={`Status: ${status.toLowerCase()}`}
    />
  );
}

function formatRelativeTime(dateStr: string | null | undefined): string | null {
  if (!dateStr) return null;
  try {
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return "just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 30) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  } catch {
    return null;
  }
}

export function ConnectionCard({
  title,
  description,
  icon,
  connected,
  enabled,
  loading,
  healthStatus,
  lastSync,
  children,
  onConnect,
  onDisconnect,
  onReconnect,
}: ConnectionCardProps) {
  const effectiveStatus: HealthStatus =
    healthStatus ?? (connected ? "HEALTHY" : "DISCONNECTED");
  const showReconnect =
    connected &&
    onReconnect &&
    (effectiveStatus === "ERROR" ||
      effectiveStatus === "DEGRADED" ||
      effectiveStatus === "DISCONNECTED");

  const relativeTime = formatRelativeTime(lastSync);

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader className="flex flex-row items-start gap-4 space-y-0">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl border bg-primary/5 text-primary">
          {icon}
        </div>
        <div className="flex-1 space-y-1">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base">{title}</CardTitle>
            <StatusDot status={effectiveStatus} />
            {connected ? (
              <Badge className="bg-primary/10 text-primary border-primary/20 hover:bg-primary/10">
                Connected
              </Badge>
            ) : (
              <Badge variant="secondary">Not connected</Badge>
            )}
          </div>
          <CardDescription>
            {description}
            {relativeTime && connected && (
              <span className="ml-1 text-xs font-mono">
                &middot; Last sync: {relativeTime}
              </span>
            )}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {children && <div className="mb-4">{children}</div>}
        <div className="flex items-center gap-2">
          {connected ? (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={onDisconnect}
                disabled={loading}
              >
                {loading && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
                Disconnect
              </Button>
              {showReconnect && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={onReconnect}
                  disabled={loading}
                  className="gap-1 border-amber-200 text-amber-700 hover:bg-amber-50"
                >
                  {loading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCw className="h-4 w-4" />
                  )}
                  Reconnect
                </Button>
              )}
            </>
          ) : onConnect ? (
            <Button
              size="sm"
              onClick={onConnect}
              disabled={loading || !enabled}
              className="bg-primary hover:bg-primary/90"
            >
              {loading && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
              Connect
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
