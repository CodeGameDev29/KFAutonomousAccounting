import { ArrowRight, X, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ChainBreadcrumbEntry } from "@/types/transaction-links";

interface ChainBreadcrumbProps {
  entries: ChainBreadcrumbEntry[];
  onNavigateBack: () => void;
  onDismiss: () => void;
}

export function ChainBreadcrumb({ entries, onNavigateBack, onDismiss }: ChainBreadcrumbProps) {
  if (entries.length === 0) return null;

  return (
    <div className="flex items-center gap-2 rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50/50 dark:bg-indigo-950/20 px-3 py-2 text-xs">
      <Button
        variant="ghost"
        size="sm"
        onClick={onNavigateBack}
        className="h-6 px-2 text-xs text-indigo-600 dark:text-indigo-400 hover:bg-indigo-100 dark:hover:bg-indigo-900/30"
      >
        <ArrowLeft className="h-3 w-3 mr-1" />
        Back
      </Button>
      <span className="text-muted-foreground/60">|</span>
      <div className="flex items-center gap-1 overflow-x-auto">
        {entries.map((entry, i) => (
          <div key={entry.transaction_id} className="flex items-center gap-1 flex-shrink-0">
            {i > 0 && <ArrowRight className="h-3 w-3 text-muted-foreground/40" />}
            <span className="rounded-full bg-indigo-100 dark:bg-indigo-900/40 px-2 py-0.5 text-indigo-700 dark:text-indigo-300 whitespace-nowrap">
              {entry.account}: ${Math.abs(parseFloat(entry.amount)).toFixed(2)} {entry.currency}
            </span>
          </div>
        ))}
      </div>
      <Button
        variant="ghost"
        size="sm"
        onClick={onDismiss}
        className="h-5 w-5 p-0 ml-auto text-muted-foreground hover:text-foreground flex-shrink-0"
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}
