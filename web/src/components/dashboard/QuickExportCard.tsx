import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { downloadBinaryOrUrl } from "@/lib/downloadUtils";
import { useAuthStore } from "@/stores/auth-store";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FileArchive, Loader2, Lock } from "lucide-react";

interface MonthStatus {
  month: number;
  transaction_count: number;
}

interface MonthlyStatusResponse {
  year: number;
  months: MonthStatus[];
}

type ExportFormat = "pdf" | "binder" | "full-year";

const FORMAT_OPTIONS: { value: ExportFormat; label: string }[] = [
  { value: "pdf", label: "PDF Report" },
  { value: "binder", label: "ZIP Audit Binder" },
  { value: "full-year", label: "Full Year ZIP" },
];

interface QuickExportCardProps {
  resolutionRate?: number;
}

export function QuickExportCard({ resolutionRate = 0 }: QuickExportCardProps) {
  const [format, setFormat] = useState<ExportFormat>("pdf");
  const [exporting, setExporting] = useState(false);

  const { data, isLoading } = useQuery<MonthlyStatusResponse>({
    queryKey: ["dashboard", "monthly-status"],
    queryFn: () => api.get("/api/dashboard/monthly-status"),
  });

  const activeMonths =
    data?.months?.filter((m) => m.transaction_count > 0) ?? [];
  const year = data?.year ?? new Date().getFullYear();

  // Return null only when truly empty (no transactions at all)
  if (!isLoading && activeMonths.length === 0) {
    return null;
  }

  // Locked state: transactions exist but reconciliation not started/complete
  if (!isLoading && resolutionRate === 0) {
    return (
      <Card className="shadow-warm rounded-xl border-l-3 border-l-muted-foreground/30">
        <CardContent className="flex items-center gap-4 p-4">
          <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-muted">
            <Lock className="h-5 w-5 text-muted-foreground" />
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-foreground">
              Quick Export
            </p>
            <p className="text-xs text-muted-foreground">
              Complete reconciliation to unlock exports
            </p>
          </div>
          <Button size="sm" className="h-8" disabled>
            <Lock className="h-3.5 w-3.5 mr-1.5" />
            Export
          </Button>
        </CardContent>
      </Card>
    );
  }

  // Find the latest month with transactions for single-month exports
  const latestMonth =
    activeMonths.length > 0
      ? activeMonths[activeMonths.length - 1].month
      : new Date().getMonth() + 1;

  const handleExport = async () => {
    setExporting(true);
    try {
      let endpoint: string;

      switch (format) {
        case "pdf":
          endpoint = `/api/reports/${year}/${latestMonth}/pdf`;
          break;
        case "binder":
          endpoint = `/api/reports/${year}/${latestMonth}/binder`;
          break;
        case "full-year":
          endpoint = `/api/reports/${year}/binder-full-year`;
          break;
      }

      // Fetch-based download so server errors are observable — a native
      // <a href> navigation swallows 403/500 with no user feedback.
      const token = await useAuthStore.getState().getAccessToken();
      const ext = format === "pdf" ? "pdf" : "zip";
      await downloadBinaryOrUrl(
        endpoint,
        "GET",
        token,
        `Export_${year}-${String(latestMonth).padStart(2, "0")}.${ext}`
      );
    } catch (err: unknown) {
      console.error("Quick export failed:", err);
      toast.error("Export failed — please try again.");
    } finally {
      setExporting(false);
    }
  };

  if (isLoading) {
    return (
      <Card className="shadow-warm rounded-xl border-l-3 border-l-success">
        <CardContent className="flex items-center gap-3 p-4">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          <span className="text-sm text-muted-foreground">
            Loading export options...
          </span>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="shadow-warm rounded-xl border-l-3 border-l-success">
      <CardContent className="flex items-center gap-4 p-4">
        <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-success/10">
          <FileArchive className="h-5 w-5 text-success" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-foreground">Quick Export</p>
          <p className="text-xs text-muted-foreground">
            Download your latest report
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <Select
            value={format}
            onValueChange={(v) => setFormat(v as ExportFormat)}
          >
            <SelectTrigger className="w-[160px] h-8 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {FORMAT_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value} className="text-xs">
                  {opt.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            size="sm"
            className="h-8 gap-1.5 bg-success hover:bg-success/90 text-white"
            onClick={handleExport}
            disabled={exporting}
          >
            {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            Export
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
