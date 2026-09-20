import { AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { classifyError } from "@/lib/error-helpers";
import { cn } from "@/lib/utils";

interface ErrorDisplayProps {
  error: unknown;
  context?: string;
  onRetry?: () => void;
  className?: string;
}

export function ErrorDisplay({ error, context, onRetry, className }: ErrorDisplayProps) {
  const classified = classifyError(error, context);
  return (
    <div className={cn("flex flex-col items-center justify-center py-16 text-center", className)}>
      <AlertCircle className="h-8 w-8 text-destructive/60 mb-3" />
      <p className="text-sm font-medium text-foreground mb-1">{classified.title}</p>
      <p className="text-xs text-muted-foreground mb-3 max-w-xs">{classified.message}</p>
      {classified.retryable && onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>Try again</Button>
      )}
    </div>
  );
}
