import { useState } from "react";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/auth-store";
import { Button } from "@/components/ui/button";
import { FolderArchive, Loader2, Check } from "lucide-react";
import { downloadBinaryOrUrl } from "@/lib/downloadUtils";

interface MonthCardExportProps {
  year: number;
  month: number;
}

export function MonthCardExport({ year, month }: MonthCardExportProps) {
  const [state, setState] = useState<"idle" | "loading" | "done">("idle");

  async function handleExport(e: React.MouseEvent) {
    e.stopPropagation();

    setState("loading");
    try {
      const token = await useAuthStore.getState().getAccessToken();
      const monthStr = String(month).padStart(2, "0");
      await downloadBinaryOrUrl(
        `/api/reports/${year}/${month}/binder`,
        "GET",
        token,
        `TaxExport_${year}-${monthStr}.zip`
      );
      setState("done");
      setTimeout(() => setState("idle"), 2000);
    } catch (err) {
      console.error("Binder export failed:", err);
      toast.error("Audit Binder export failed — please try again.");
      setState("idle");
    }
  }

  return (
    <Button
      variant="ghost"
      size="sm"
      className="gap-1.5 h-8 text-xs text-muted-foreground hover:text-foreground"
      disabled={state === "loading"}
      onClick={handleExport}
      title="Download the audit binder (ZIP) for this month"
      aria-label={`Export month ${month} audit binder ZIP`}
    >
      {state === "loading" ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : state === "done" ? (
        <Check className="h-3.5 w-3.5 text-primary" />
      ) : (
        <FolderArchive className="h-3.5 w-3.5" />
      )}
      {state === "done" ? "Done" : "Audit Binder"}
    </Button>
  );
}
