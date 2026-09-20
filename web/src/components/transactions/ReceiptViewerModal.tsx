import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Loader2, Download, AlertCircle } from "lucide-react";
import { API_URL } from "@/lib/constants";
import { useAuthStore } from "@/stores/auth-store";

interface ReceiptViewerModalProps {
  documentId: number | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Renders a receipt PDF/image inline inside a modal.
 *
 * `window.open(blobUrl, "_blank")` is not dependable here: ad-blockers, popup
 * blockers and "block new tabs" extensions swallow the call silently, leaving
 * the user with nothing. The fetched blob is therefore rendered in an <iframe>
 * (PDFs/HTML) or an <img> (images) inside the page itself.
 */
export function ReceiptViewerModal({
  documentId,
  open,
  onOpenChange,
}: ReceiptViewerModalProps) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [contentType, setContentType] = useState<string | null>(null);
  const [filename, setFilename] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || documentId == null) {
      return;
    }
    let cancelled = false;
    let createdUrl: string | null = null;

    setLoading(true);
    setError(null);
    setBlobUrl(null);
    setContentType(null);
    setFilename(null);

    (async () => {
      try {
        const token = await useAuthStore.getState().getAccessToken();
        const res = await fetch(`${API_URL}/api/receipts/${documentId}/download`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          redirect: "follow",
        });
        if (!res.ok) {
          throw new Error(`Failed to fetch receipt (HTTP ${res.status})`);
        }
        const blob = await res.blob();
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        createdUrl = url;
        const cd = res.headers.get("Content-Disposition") ?? "";
        const fnMatch = /filename\*?=(?:UTF-8'')?["]?([^";]+)/i.exec(cd);
        setBlobUrl(url);
        setContentType(blob.type || res.headers.get("Content-Type"));
        setFilename(fnMatch?.[1] ? decodeURIComponent(fnMatch[1]) : `receipt-${documentId}`);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load receipt");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      // Revoke after the iframe/img has had a chance to release the URL.
      if (createdUrl) {
        setTimeout(() => URL.revokeObjectURL(createdUrl as string), 0);
      }
    };
  }, [open, documentId]);

  const isImage = !!contentType && contentType.startsWith("image/");
  const isPdfOrHtml =
    !!contentType &&
    (contentType.includes("pdf") ||
      contentType.includes("html") ||
      contentType === "application/octet-stream");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl w-[95vw] h-[90vh] flex flex-col p-0 overflow-hidden">
        <DialogHeader className="px-4 py-3 border-b flex-row items-center justify-between space-y-0">
          <div className="flex-1 min-w-0">
            <DialogTitle className="text-sm font-semibold truncate">
              {filename ?? "Receipt"}
            </DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              Preview inline — no new tab needed
            </DialogDescription>
          </div>
          {/* mr-8 keeps Download clear of the absolute right-4 close X
              that DialogContent renders into every dialog. */}
          {blobUrl && filename && (
            <Button
              variant="outline"
              size="sm"
              asChild
              className="ml-2 mr-8 shrink-0"
            >
              <a href={blobUrl} download={filename}>
                <Download className="h-3.5 w-3.5 mr-1.5" />
                Download
              </a>
            </Button>
          )}
        </DialogHeader>

        <div className="flex-1 min-h-0 bg-muted/20 flex items-center justify-center overflow-auto">
          {loading ? (
            <div className="flex flex-col items-center gap-3 text-muted-foreground">
              <Loader2 className="h-8 w-8 animate-spin" />
              <p className="text-sm">Loading receipt…</p>
            </div>
          ) : error ? (
            <div className="flex flex-col items-center gap-3 text-destructive p-6 text-center">
              <AlertCircle className="h-8 w-8" />
              <p className="text-sm font-medium">{error}</p>
              <p className="text-xs text-muted-foreground max-w-xs">
                If your browser is blocking content, try refreshing the page.
              </p>
            </div>
          ) : blobUrl && isImage ? (
            <img
              src={blobUrl}
              alt={filename ?? "Receipt"}
              className="max-h-full max-w-full object-contain"
            />
          ) : blobUrl && isPdfOrHtml ? (
            <iframe
              src={blobUrl}
              title={filename ?? "Receipt"}
              className="w-full h-full border-0"
            />
          ) : blobUrl ? (
            <div className="flex flex-col items-center gap-3 text-muted-foreground p-6 text-center">
              <p className="text-sm">
                Inline preview not supported for this file type.
              </p>
              <Button asChild>
                <a href={blobUrl} download={filename ?? `receipt-${documentId}`}>
                  <Download className="h-4 w-4 mr-1.5" />
                  Download to view
                </a>
              </Button>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
