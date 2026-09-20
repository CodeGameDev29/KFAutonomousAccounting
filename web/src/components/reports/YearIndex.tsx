/** Year index — lists available fiscal years with indigo accent styling. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { CalendarDays, ChevronRight, BarChart3, Link2, Trash2, ExternalLink } from "lucide-react";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import { useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

interface YearSummary {
  year: number;
  total_transactions: number;
  total_accounts: number;
  months_with_data: number;
  closed_months: number;
}

interface YearIndexResponse {
  years: YearSummary[];
}

interface SharedLink {
  token: string;
  year: number;
  month: number;
  created_at: string;
  expires_at: string;
  is_expired: boolean;
}

function SharedLinksPanel() {
  const queryClient = useQueryClient();
  const [revokeTarget, setRevokeTarget] = useState<string | null>(null);

  const { data: linksData } = useQuery<{ links: SharedLink[] }>({
    queryKey: ["shared-links"],
    queryFn: () => api.get("/api/reports/shared-links"),
    staleTime: 30_000,
  });

  const revokeMutation = useMutation({
    mutationFn: (token: string) => api.delete(`/api/reports/shared/${token}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["shared-links"] });
    },
  });

  const links = linksData?.links ?? [];
  if (links.length === 0) return null;

  const activeLinks = links.filter((l) => !l.is_expired);
  const expiredLinks = links.filter((l) => l.is_expired);

  const monthName = (m: number) =>
    new Date(2000, m - 1).toLocaleString("en-CA", { month: "short" });

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Link2 className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold text-foreground">
          Shared Report Links
        </h3>
        <Badge variant="secondary" className="text-[10px]">
          {activeLinks.length} active
        </Badge>
      </div>
      <div className="space-y-2">
        {activeLinks.map((link) => (
          <div
            key={link.token}
            className="flex items-center justify-between rounded-lg border border-border/60 bg-card p-3 shadow-warm-sm"
          >
            <div className="flex items-center gap-3 min-w-0">
              <ExternalLink className="h-4 w-4 text-primary shrink-0" />
              <div className="min-w-0">
                <p className="text-sm font-medium">
                  {monthName(link.month)} {link.year}
                </p>
                <p className="text-xs text-muted-foreground">
                  Expires {new Date(link.expires_at).toLocaleDateString("en-CA", {
                    month: "short",
                    day: "numeric",
                    hour: "numeric",
                    minute: "2-digit",
                  })}
                </p>
              </div>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs text-destructive hover:bg-destructive/10 hover:text-destructive"
              onClick={() => setRevokeTarget(link.token)}
              disabled={revokeMutation.isPending}
            >
              <Trash2 className="h-3.5 w-3.5 mr-1" />
              Revoke
            </Button>
          </div>
        ))}
        {expiredLinks.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {expiredLinks.length} expired link{expiredLinks.length !== 1 ? "s" : ""} (auto-cleaned)
          </p>
        )}
      </div>

      {/* Revoke shared link confirm dialog */}
      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
        title="Revoke shared link?"
        description="Anyone with this link will lose access to the shared report."
        confirmLabel="Revoke"
        variant="destructive"
        isPending={revokeMutation.isPending}
        onConfirm={() => {
          if (revokeTarget) {
            revokeMutation.mutate(revokeTarget, {
              onSettled: () => setRevokeTarget(null),
            });
          }
        }}
      />
    </div>
  );
}

export function YearIndex() {
  const navigate = useNavigate();

  const { data, isLoading, isError, error, refetch } = useQuery<YearIndexResponse>({
    queryKey: ["reports", "years"],
    queryFn: () => api.get("/api/reports/years"),
  });

  return (
    <div className="space-y-6 pt-4">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Reports</h1>
        <p className="mt-1 text-muted-foreground">
          Select a fiscal year to view monthly reports and transaction details.
        </p>
      </div>

      {isError ? (
        <ErrorDisplay error={error} context="reports" onRetry={refetch} />
      ) : isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i} className="shadow-warm rounded-xl">
              <CardContent className="p-6 space-y-3">
                <Skeleton className="h-8 w-20" />
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 w-32" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : !data || data.years.length === 0 ? (
        <Card className="shadow-warm rounded-xl">
          <CardContent className="flex h-[300px] flex-col items-center justify-center gap-3 p-6">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
              <BarChart3 className="h-7 w-7 text-primary" />
            </div>
            <div className="text-center">
              <p className="font-medium">No reports yet</p>
              <p className="text-sm text-muted-foreground">
                Upload bank statements and receipts to generate your first report.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.years.map((yr) => (
            <Card
              key={yr.year}
              className="shadow-warm rounded-xl card-hover cursor-pointer accent-border-left"
              onClick={() => navigate(`/reports/${yr.year}`)}
            >
              <CardContent className="p-6">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10">
                      <CalendarDays className="h-5 w-5 text-primary" />
                    </div>
                    <div>
                      <h3 className="text-2xl font-bold tracking-tight">{yr.year}</h3>
                    </div>
                  </div>
                  <ChevronRight className="h-5 w-5 text-muted-foreground" />
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <Badge className="bg-primary/10 text-primary border-primary/20 hover:bg-primary/10">
                    <span className="font-mono">{yr.months_with_data}</span>
                    <span className="ml-1">{yr.months_with_data === 1 ? "month" : "months"}</span>
                  </Badge>
                  <Badge variant="secondary">
                    <span className="font-mono">{yr.total_transactions}</span>
                    <span className="ml-1">{yr.total_transactions === 1 ? "transaction" : "transactions"}</span>
                  </Badge>
                  {yr.closed_months > 0 && (
                    <Badge className="bg-success/10 text-success border-success/20 hover:bg-success/10">
                      <span className="font-mono">{yr.closed_months}</span>
                      <span className="ml-1">closed</span>
                    </Badge>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Shared link management */}
      <SharedLinksPanel />
    </div>
  );
}
