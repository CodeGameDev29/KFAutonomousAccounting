import { ChevronLeft, ChevronRight, Calendar } from "lucide-react";
import { Button } from "@/components/ui/button";

interface PeriodSelectorProps {
  monthLabel: string;
  onPrev: () => void;
  onNext: () => void;
  canGoNext: boolean;
}

export function PeriodSelector({
  monthLabel,
  onPrev,
  onNext,
  canGoNext,
}: PeriodSelectorProps) {
  return (
    <div className="flex items-center gap-2">
      <Calendar className="h-4 w-4 text-muted-foreground" />
      <Button
        variant="ghost"
        size="sm"
        className="h-7 w-7 p-0"
        onClick={onPrev}
        aria-label="Previous month"
      >
        <ChevronLeft className="h-4 w-4" />
      </Button>
      <span className="text-sm font-semibold text-foreground min-w-[130px] text-center">
        {monthLabel}
      </span>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 w-7 p-0"
        onClick={onNext}
        disabled={!canGoNext}
        aria-label="Next month"
      >
        <ChevronRight className="h-4 w-4" />
      </Button>
    </div>
  );
}
