import { useState } from "react";
import { cn } from "@/lib/utils";
import { ChevronDown, ChevronUp, BarChart3 } from "lucide-react";
import { Button } from "@/components/ui/button";

interface SubScore {
  label: string;
  score: number; // 0-100
  weight: number;
  detail: string;
}

interface MatchExplanationProps {
  explanation: string;
  subScores?: SubScore[];
  collapsible?: boolean;
}

function ScoreBar({ label, score, weight, detail }: SubScore) {
  const barColor =
    score >= 80
      ? "bg-primary"
      : score >= 60
      ? "bg-amber-500"
      : "bg-destructive";

  const bgTrackColor =
    score >= 80
      ? "bg-primary/10"
      : score >= 60
      ? "bg-amber-100 dark:bg-amber-950/30"
      : "bg-destructive/10";

  return (
    <div className="space-y-1.5" title={detail}>
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground font-medium">
          {label}{" "}
          <span className="text-muted-foreground/40">
            ({Math.round(weight * 100)}%)
          </span>
        </span>
        <span
          className={cn(
            "font-mono font-bold tabular-nums",
            score >= 80
              ? "text-primary"
              : score >= 60
              ? "text-amber-600"
              : "text-destructive"
          )}
        >
          {Math.round(score)}
        </span>
      </div>
      <div className={cn("h-2 w-full rounded-full", bgTrackColor)}>
        <div
          className={cn(
            "h-2 rounded-full transition-all duration-500 ease-out",
            barColor
          )}
          style={{ width: `${Math.min(score, 100)}%` }}
        />
      </div>
      {detail && (
        <p className="text-[11px] text-muted-foreground/50 leading-tight">
          {detail}
        </p>
      )}
    </div>
  );
}

export function MatchExplanation({
  explanation,
  subScores,
  collapsible = true,
}: MatchExplanationProps) {
  const [expanded, setExpanded] = useState(true);

  const hasSubScores = subScores && subScores.length > 0;

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground leading-relaxed">
        {explanation}
      </p>

      {hasSubScores && collapsible && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs text-muted-foreground hover:text-primary hover:bg-primary/5 gap-1"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? (
            <>
              Hide details
              <ChevronUp className="h-3 w-3" />
            </>
          ) : (
            <>
              <BarChart3 className="h-3 w-3" />
              Show score breakdown
              <ChevronDown className="h-3 w-3" />
            </>
          )}
        </Button>
      )}

      {hasSubScores && expanded && (
        <div className="space-y-4 rounded-xl border border-border/40 bg-card p-4 shadow-warm-sm">
          {subScores.map((sub) => (
            <ScoreBar key={sub.label} {...sub} />
          ))}
        </div>
      )}
    </div>
  );
}
