import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useConfidenceBands, type ConfidenceBands } from "@/hooks/use-system-info";

interface ConfidenceIndicatorProps {
  confidence: number; // 0-100
  explanation?: string;
  variant?: "compact" | "expanded";
}

/**
 * Where a score sits relative to this instance's bands.
 *
 * `bands` come from `/health`; the engine's thresholds are env-tunable, so the
 * only correct source for them is the server. When they are not known yet the
 * dots render the score itself — four quarters of 0-100 — and the tooltip
 * names no band at all, because naming one would be a guess.
 */
export function getConfidenceLevel(confidence: number, bands: ConfidenceBands | null) {
  if (!bands) {
    const filled = Math.min(4, Math.max(1, Math.ceil(confidence / 25)));
    return { filled, color: "bg-primary", label: null as string | null };
  }
  if (confidence >= bands.high) return { filled: 4, color: "bg-success", label: "High" };
  if (confidence >= bands.medium) return { filled: 3, color: "bg-amber-500", label: "Medium" };
  return { filled: 2, color: "bg-red-500", label: "Low" };
}

function getTooltipText(
  label: string | null,
  confidence: number,
  bands: ConfidenceBands | null
): string {
  const score = `${Math.round(confidence)}%`;
  if (!label || !bands) {
    return `Match score ${score}. This instance has not reported its confidence bands.`;
  }
  switch (label) {
    case "High":
      return `HIGH confidence — ${bands.high}% or more, approved without review (${score})`;
    case "Medium":
      return `MEDIUM confidence — ${bands.medium}% to ${bands.high - 1}%, waits for your review (${score})`;
    default:
      return `LOW confidence — below ${bands.medium}%, may need manual matching (${score})`;
  }
}

export function ConfidenceIndicator({
  confidence,
  explanation,
  variant = "compact",
}: ConfidenceIndicatorProps) {
  const bands = useConfidenceBands();
  const { filled, color, label } = getConfidenceLevel(confidence, bands);
  const tooltip = explanation ?? getTooltipText(label, confidence, bands);

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div className="inline-flex items-center gap-1.5 cursor-default">
            <div className="flex items-center gap-0.5">
              {Array.from({ length: 4 }).map((_, i) => (
                <div
                  key={i}
                  className={cn(
                    "h-2 w-2 rounded-full",
                    i < filled ? color : "bg-muted-foreground/20"
                  )}
                />
              ))}
            </div>
            {variant === "expanded" && (
              <span className="text-xs tabular-nums text-muted-foreground">
                {Math.round(confidence)}%
              </span>
            )}
          </div>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs max-w-[240px]">
          {tooltip}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
