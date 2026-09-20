import { Lightbulb, Check, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

// Deterministic backend `recommendation` prose in muted bookkeeping-lane
// styling. Text is rendered VERBATIM — never template extra prose (all copy
// originates server-side and has already passed _reject_forbidden_language).
export interface RecommendationLineProps {
  text: string;
  flagKey?: string | null;
  onDismiss?: (id: string) => void;
  dismissing?: boolean;
}

export function RecommendationLine({
  text,
  flagKey,
  onDismiss,
  dismissing,
}: RecommendationLineProps) {
  return (
    <div className="flex items-start gap-2 rounded-md bg-muted/30 px-3 py-2">
      <Lightbulb className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
      <p className="min-w-0 flex-1 text-sm text-muted-foreground">{text}</p>
      {flagKey && onDismiss && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 flex-shrink-0 text-xs text-muted-foreground hover:text-foreground"
          disabled={dismissing}
          onClick={() => onDismiss(flagKey)}
        >
          {dismissing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <>
              <Check className="mr-1 h-3.5 w-3.5" />
              Expected
            </>
          )}
        </Button>
      )}
    </div>
  );
}
