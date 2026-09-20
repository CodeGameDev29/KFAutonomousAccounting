/** Activity Log — displays audit trail of reconciliation actions. */

import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  CheckCircle,
  XCircle,
  Unlink,
  FileText,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
  Calendar,
  Link2,
  Upload,
  Download,
  Loader2,
  Tag,
  StickyNote,
  Trash2,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { withParams } from "@/lib/url-state";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import { useAuthStore } from "@/stores/auth-store";

interface AuditEntry {
  id: number;
  entity_type: string;
  entity_id: number;
  action: string;
  old_value: string | null;
  new_value: string | null;
  performed_by: string | null;
  created_at: string | null;
}

interface AuditResponse {
  entries: AuditEntry[];
  total: number;
  page: number;
  per_page: number;
}

const ACTION_CONFIG: Record<
  string,
  { icon: typeof CheckCircle; label: string; color: string }
> = {
  APPROVE: {
    icon: CheckCircle,
    label: "Approved",
    color: "bg-primary/10 text-primary border-primary/20",
  },
  REJECT: {
    icon: XCircle,
    label: "Rejected",
    color: "bg-destructive/10 text-destructive border-destructive/20",
  },
  UNMATCH: {
    icon: Unlink,
    label: "Unmatched",
    color: "bg-amber-50 text-amber-700 border-amber-200",
  },
  AUTO_APPROVE: {
    icon: CheckCircle,
    label: "Auto-matched",
    color: "bg-primary/10 text-primary border-primary/20",
  },
  RUN_RECONCILIATION: {
    icon: RefreshCw,
    label: "Match Run",
    color: "bg-blue-50 text-blue-700 border-blue-200",
  },
  CORRECT: {
    icon: FileText,
    label: "Corrected",
    color: "bg-amber-50 text-amber-700 border-amber-200",
  },
  SKIP_STEP: {
    icon: FileText,
    label: "Step Skipped",
    color: "bg-muted text-muted-foreground border-border",
  },
  RECONNECT: {
    icon: RefreshCw,
    label: "Reconnected",
    color: "bg-blue-50 text-blue-700 border-blue-200",
  },
  MANUAL_MATCH: {
    icon: Link2,
    label: "Manual Match",
    color: "bg-primary/10 text-primary border-primary/20",
  },
  REASSIGN: {
    icon: RefreshCw,
    label: "Reassigned",
    color: "bg-blue-50 text-blue-700 border-blue-200",
  },
  LINK: {
    icon: Link2,
    label: "Linked",
    color: "bg-primary/10 text-primary border-primary/20",
  },
  UNLINK: {
    icon: Unlink,
    label: "Unlinked",
    color: "bg-amber-50 text-amber-700 border-amber-200",
  },
  REMOVE_PROOF: {
    icon: XCircle,
    label: "Receipt Removed",
    color: "bg-destructive/10 text-destructive border-destructive/20",
  },
  ADD_PROOF: {
    icon: CheckCircle,
    label: "Receipt Added",
    color: "bg-primary/10 text-primary border-primary/20",
  },
  UPLOAD_RECEIPT: {
    icon: Upload,
    label: "Receipt Uploaded",
    color: "bg-primary/10 text-primary border-primary/20",
  },
  UPLOAD_STATEMENT: {
    icon: Upload,
    label: "Statement Uploaded",
    color: "bg-blue-50 text-blue-700 border-blue-200",
  },
  RECATEGORIZE: {
    icon: Tag,
    label: "Recategorized",
    color: "bg-amber-50 text-amber-700 border-amber-200",
  },
  EDIT_NOTE: {
    icon: StickyNote,
    label: "Note Edited",
    color: "bg-amber-50 text-amber-700 border-amber-200",
  },
  DELETE_RECEIPT: {
    icon: Trash2,
    label: "Receipt Deleted",
    color: "bg-destructive/10 text-destructive border-destructive/20",
  },
  DELETE_STATEMENT: {
    icon: Trash2,
    label: "Statement Deleted",
    color: "bg-destructive/10 text-destructive border-destructive/20",
  },
};

function formatTimestamp(ts: string | null): string {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Return YYYY-MM-DD for the 1st of the current month. */
function monthStart(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
}

/** Return YYYY-MM-DD for today. */
function today(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function ActionBadge({ action }: { action: string }) {
  const config = ACTION_CONFIG[action] || {
    icon: FileText,
    label: action,
    color: "bg-muted text-muted-foreground border-border",
  };
  const Icon = config.icon;
  return (
    <Badge className={`${config.color} font-medium`}>
      <Icon className="h-3 w-3 mr-1" />
      {config.label}
    </Badge>
  );
}

function EntityBadge({ type }: { type: string }) {
  return (
    <Badge variant="outline" className="text-xs font-mono">
      {type}
    </Badge>
  );
}

export function Activity() {
  // Range + page live in the URL so back/forward restores them. Absent
  // param = default range; the literal "all" encodes "All time" (empty).
  const [searchParams, setSearchParams] = useSearchParams();
  const page = Math.max(1, Number(searchParams.get("page")) || 1);
  const perPage = 25;
  const fromParam = searchParams.get("from");
  const toParam = searchParams.get("to");
  const startDate =
    fromParam === null ? monthStart() : fromParam === "all" ? "" : fromParam;
  const endDate = toParam === null ? today() : toParam === "all" ? "" : toParam;
  const setPage = useCallback(
    (p: number) =>
      setSearchParams((prev) =>
        withParams(prev, { page: p <= 1 ? null : String(p) }),
      ),
    [setSearchParams],
  );
  // Presets push (navigational); typing in the date inputs replaces.
  const setRange = useCallback(
    (from: string, to: string, opts?: { replace?: boolean }) =>
      setSearchParams(
        (prev) =>
          withParams(prev, {
            from: from === "" ? "all" : from,
            to: to === "" ? "all" : to,
            page: null,
          }),
        opts,
      ),
    [setSearchParams],
  );
  const [isExporting, setIsExporting] = useState(false);

  async function handleExportCSV() {
    setIsExporting(true);
    try {
      const params = new URLSearchParams();
      if (startDate) params.set("start_date", startDate);
      if (endDate) params.set("end_date", endDate);
      const token = await useAuthStore.getState().getAccessToken();
      const resp = await fetch(
        `${API_URL}/api/reconciliation/audit-log/export?${params}`,
        {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        }
      );
      if (!resp.ok) throw new Error("Export failed");
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `audit-log-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Audit-log CSV export failed:", err);
      toast.error("CSV export failed — please try again.");
    } finally {
      setIsExporting(false);
    }
  }

  const queryParams = useMemo(() => {
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("per_page", String(perPage));
    if (startDate) params.set("start_date", startDate);
    if (endDate) params.set("end_date", endDate);
    return params.toString();
  }, [page, perPage, startDate, endDate]);

  const { data, isLoading, isError, error, refetch } = useQuery<AuditResponse>({
    queryKey: ["audit-log", page, startDate, endDate],
    queryFn: () => api.get(`/api/reconciliation/audit-log?${queryParams}`),
  });

  const entries = data?.entries ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / perPage);

  if (isError) {
    return (
      <ErrorDisplay error={error} context="activity log" onRetry={refetch} />
    );
  }

  return (
    <div className="space-y-6 pt-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold tracking-tight text-foreground">
            Activity Log
          </h2>
          <p className="text-muted-foreground">
            Audit trail of all actions — approvals, rejections, matches,
            links, and corrections.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleExportCSV}
          disabled={isExporting}
          className="gap-1.5 shrink-0"
        >
          {isExporting ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Download className="h-4 w-4" />
          )}
          Download CSV
        </Button>
      </div>

      {/* Date range filter */}
      <div className="flex items-end gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <Calendar className="h-4 w-4 text-muted-foreground shrink-0" />
          <div className="grid gap-1">
            <Label htmlFor="start-date" className="text-xs text-muted-foreground">
              From
            </Label>
            <Input
              id="start-date"
              type="date"
              value={startDate}
              onChange={(e) =>
                setRange(e.target.value, endDate, { replace: true })
              }
              className="h-9 w-40 text-sm"
            />
          </div>
        </div>
        <div className="grid gap-1">
          <Label htmlFor="end-date" className="text-xs text-muted-foreground">
            To
          </Label>
          <Input
            id="end-date"
            type="date"
            value={endDate}
            onChange={(e) =>
              setRange(startDate, e.target.value, { replace: true })
            }
            className="h-9 w-40 text-sm"
          />
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-9"
          onClick={() => setRange(monthStart(), today())}
        >
          This month
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-9"
          onClick={() => setRange(`${new Date().getFullYear()}-01-01`, today())}
        >
          Year to date
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-9"
          onClick={() => setRange("", "")}
        >
          All time
        </Button>
      </div>

      <Card className="shadow-warm rounded-xl overflow-hidden">
        <CardHeader className="pb-2">
          <CardTitle className="text-lg font-semibold flex items-center justify-between">
            <span>
              Activity{" "}
              {total > 0 && (
                <span className="text-sm font-normal text-muted-foreground">
                  ({total} {total === 1 ? "entry" : "entries"})
                </span>
              )}
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="space-y-3 p-6">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : entries.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 px-6 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-muted mb-3">
                <FileText className="h-6 w-6 text-muted-foreground/40" />
              </div>
              <p className="text-sm font-medium text-muted-foreground">
                No activity in this date range
              </p>
              <p className="text-xs text-muted-foreground/60 mt-1">
                Try expanding the date range or click "All time" to see
                everything.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-border/50">
              {entries.map((entry) => (
                <div
                  key={entry.id}
                  className="flex items-center gap-4 px-6 py-3 hover:bg-muted/20 transition-colors"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <ActionBadge action={entry.action} />
                      <EntityBadge type={entry.entity_type} />
                      <span className="text-xs text-muted-foreground font-mono">
                        #{entry.entity_id}
                      </span>
                    </div>
                    {(entry.old_value || entry.new_value) && (
                      <p className="text-xs text-muted-foreground mt-1 truncate">
                        {entry.old_value && (
                          <span>
                            From: <span className="font-mono">{entry.old_value}</span>
                          </span>
                        )}
                        {entry.old_value && entry.new_value && " → "}
                        {entry.new_value && (
                          <span>
                            To: <span className="font-mono">{entry.new_value}</span>
                          </span>
                        )}
                      </p>
                    )}
                  </div>
                  <div className="text-right shrink-0">
                    <p className="text-xs text-muted-foreground whitespace-nowrap">
                      {formatTimestamp(entry.created_at)}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t px-6 py-3">
              <p className="text-xs text-muted-foreground">
                Page {page} of {totalPages} ({total} entries)
              </p>
              <div className="flex gap-1">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage(page - 1)}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage(page + 1)}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
