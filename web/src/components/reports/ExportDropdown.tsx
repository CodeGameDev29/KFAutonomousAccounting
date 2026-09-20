import { useState } from "react";
import { toast } from "sonner";
import { downloadBinaryOrUrl } from "@/lib/downloadUtils";
import { useAuthStore } from "@/stores/auth-store";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Download,
  FileText,
  FileSpreadsheet,
  FolderArchive,
  Loader2,
} from "lucide-react";

interface ExportDropdownProps {
  year: number;
  month: number;
}

type ExportFormat = "pdf" | "xlsx" | "binder" | "qbo" | "xero";

interface FormatOption {
  key: ExportFormat;
  label: string;
  description: string;
  icon: React.ComponentType<{ className?: string }>;
  endpoint: (year: number, month: number) => string;
}

const FORMAT_OPTIONS: FormatOption[] = [
  {
    key: "pdf",
    label: "PDF Report",
    description: "Summary, category P&L and per-account tables",
    icon: FileText,
    endpoint: (y, m) => `/api/reports/${y}/${m}/pdf`,
  },
  {
    key: "binder",
    label: "Audit Binder (ZIP)",
    description: "Ledger workbook + receipts + statements for CRA",
    icon: FolderArchive,
    endpoint: (y, m) => `/api/reports/${y}/${m}/binder`,
  },
  {
    key: "qbo",
    label: "QuickBooks CSV",
    description: "Import transactions into QuickBooks Online",
    icon: FileSpreadsheet,
    endpoint: (y, m) => `/api/reports/${y}/${m}/export-qbo`,
  },
  {
    key: "xero",
    label: "Xero CSV",
    description: "Import transactions into Xero",
    icon: FileSpreadsheet,
    endpoint: (y, m) => `/api/reports/${y}/${m}/export-xero`,
  },
];

export function ExportDropdown({ year, month }: ExportDropdownProps) {
  const [activeExport, setActiveExport] = useState<ExportFormat | null>(null);

  const handleExport = async (format: FormatOption) => {
    setActiveExport(format.key);
    try {
      const endpoint = format.endpoint(year, month);
      // Fetch-based download (not <a href> navigation) so server errors are
      // observable — a native navigation swallows 403/500 and the button
      // just silently does nothing.
      const token = await useAuthStore.getState().getAccessToken();
      const monthStr = String(month).padStart(2, "0");
      await downloadBinaryOrUrl(
        endpoint,
        "GET",
        token,
        `Export_${year}-${monthStr}.${format.key === "binder" ? "zip" : format.key === "pdf" ? "pdf" : "csv"}`
      );
    } catch (err: unknown) {
      console.error("Export failed:", err);
      toast.error(
        `${format.label} export failed${err instanceof Error && err.message ? ` — ${err.message}` : ""}`
      );
    } finally {
      setActiveExport(null);
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="gap-2 border-primary/20 text-primary hover:bg-primary/5"
        >
          {activeExport ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Download className="h-4 w-4" />
          )}
          Export
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72 shadow-warm rounded-xl">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
          Export {new Date(year, month - 1).toLocaleString("en-CA", { month: "long" })} {year} — all
          accounts &amp; currencies included
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {FORMAT_OPTIONS.map((format) => {
          const Icon = format.icon;
          const isActive = activeExport === format.key;

          return (
            <DropdownMenuItem
              key={format.key}
              disabled={activeExport !== null}
              onClick={() => handleExport(format)}
              className="flex items-start gap-3 py-2 cursor-pointer"
            >
              <div className="mt-0.5">
                {isActive ? (
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                ) : (
                  <Icon className="h-4 w-4 text-primary" />
                )}
              </div>
              <div className="flex flex-col flex-1">
                <span className="text-sm font-medium">{format.label}</span>
                <span className="text-xs text-muted-foreground">
                  {format.description}
                </span>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
