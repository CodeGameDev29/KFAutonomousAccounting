import { useState, useCallback, useSyncExternalStore } from "react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useConfidenceBands } from "@/hooks/use-system-info";

const STORAGE_KEY = "confidence-education-seen";

function getSnapshot(): boolean {
  return localStorage.getItem(STORAGE_KEY) === "true";
}

function subscribe(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  return () => window.removeEventListener("storage", callback);
}

export function useConfidenceEducation() {
  const seen = useSyncExternalStore(subscribe, getSnapshot, () => true);

  const dismiss = useCallback(() => {
    localStorage.setItem(STORAGE_KEY, "true");
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
  }, []);

  return { seen, dismiss };
}

interface ConfidenceEducationPopoverProps {
  children: React.ReactNode;
}

export function ConfidenceEducationPopover({
  children,
}: ConfidenceEducationPopoverProps) {
  const { seen, dismiss } = useConfidenceEducation();
  const [open, setOpen] = useState(!seen);
  // The bands are the engine's, and they are env-tunable per instance — so
  // they are read from the server, never written into this copy.
  const bands = useConfidenceBands();

  if (seen) return <>{children}</>;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent className="w-80 text-sm" side="bottom" align="start">
        <div className="space-y-3">
          <p className="font-semibold text-foreground">
            Understanding confidence scores
          </p>
          <p className="text-muted-foreground leading-relaxed">
            A confidence score is how strongly the matching engine rates a
            receipt against a transaction.
          </p>
          {bands ? (
            <div className="space-y-1.5 text-muted-foreground">
              <p>
                <span className="font-medium text-foreground">HIGH</span> (
                {bands.high}%+) = approved without review
              </p>
              <p>
                <span className="font-medium text-foreground">MEDIUM</span> (
                {bands.medium}&ndash;{bands.high - 1}%) = needs your review
              </p>
              <p>
                <span className="font-medium text-foreground">LOW</span> (&lt;
                {bands.medium}%) = may need manual matching
              </p>
              <p className="text-xs">
                These thresholds are set on the server running this instance.
              </p>
            </div>
          ) : (
            <div className="space-y-1.5 text-muted-foreground">
              <p>
                A higher score means a stronger match. Above the server&apos;s
                auto-approve threshold the engine stores the match itself; below
                it the pair waits here for you.
              </p>
              <p className="text-xs">
                This instance has not reported its thresholds, so no percentages
                are shown.
              </p>
            </div>
          )}
          <Button
            size="sm"
            className="w-full"
            onClick={() => {
              dismiss();
              setOpen(false);
            }}
          >
            Got it
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
