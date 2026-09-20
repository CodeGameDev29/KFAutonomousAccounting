import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Check, Copy, Link2, Loader2 } from "lucide-react";
import type { ShareLinkResponse } from "./types";
import { formatDatetime } from "./format";

interface ShareDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  year: number;
  month: number;
  monthLabel: string;
}

export function ShareDialog({
  open,
  onOpenChange,
  year,
  month,
  monthLabel,
}: ShareDialogProps) {
  const [copied, setCopied] = useState(false);
  const [hours, setHours] = useState(72);

  const shareMutation = useMutation({
    mutationFn: () =>
      api.post<ShareLinkResponse>(
        `/api/reports/${year}/${month}/share?hours=${hours}`
      ),
    onSuccess: () => {
      setCopied(false);
    },
  });

  const shareData = shareMutation.data;

  function handleCopy() {
    if (!shareData?.url) return;
    navigator.clipboard.writeText(shareData.url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  function handleOpenChange(next: boolean) {
    if (!next) {
      // Reset state when closing
      shareMutation.reset();
      setCopied(false);
      setHours(72);
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md shadow-warm-md rounded-xl">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold">Share Report</DialogTitle>
          <DialogDescription>
            Generate a shareable link for the {monthLabel} report. Anyone with
            the link can view the report until it expires.
          </DialogDescription>
        </DialogHeader>

        {!shareData ? (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="share-hours">
                Link expires after
              </label>
              <div className="flex items-center gap-2">
                <Input
                  id="share-hours"
                  type="number"
                  min={1}
                  max={720}
                  value={hours}
                  onChange={(e) => setHours(Number(e.target.value))}
                  className="w-24 font-mono focus-visible:ring-primary"
                />
                <span className="text-sm text-muted-foreground">hours</span>
              </div>
            </div>
            <DialogFooter>
              <Button
                onClick={() => shareMutation.mutate()}
                disabled={shareMutation.isPending}
                className="bg-primary hover:bg-primary/90"
              >
                {shareMutation.isPending ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Generating...
                  </>
                ) : (
                  <>
                    <Link2 className="mr-2 h-4 w-4" />
                    Generate Link
                  </>
                )}
              </Button>
            </DialogFooter>
            {shareMutation.isError && (
              <p className="text-sm text-rose-600">
                Failed to generate share link. Please try again.
              </p>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium">Share link</label>
              <div className="flex items-center gap-2">
                <Input
                  readOnly
                  value={shareData.url}
                  className="font-mono text-xs bg-muted/30"
                />
                <Button
                  variant="outline"
                  size="icon"
                  onClick={handleCopy}
                  className="shrink-0 border-primary/20 hover:bg-primary/5"
                >
                  {copied ? (
                    <Check className="h-4 w-4 text-primary" />
                  ) : (
                    <Copy className="h-4 w-4 text-primary" />
                  )}
                </Button>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              This link expires on{" "}
              <span className="font-medium font-mono">
                {formatDatetime(shareData.expires_at)}
              </span>
              . Anyone with this link can view the report in read-only mode.
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
