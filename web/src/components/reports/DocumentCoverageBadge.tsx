import { Badge } from "@/components/ui/badge";
import { FileCheck } from "lucide-react";

/** Above this share of documented transactions the badge is worth showing. */
export const WELL_DOCUMENTED_THRESHOLD = 80; // percent

interface DocumentCoverageBadgeProps {
  className?: string;
  matchRate?: number; // 0-100
}

/**
 * The share of a period's transactions that have a matched source document,
 * shown beside binder downloads and export surfaces.
 *
 * It states coverage and nothing more. Nothing here has been assessed against
 * the CRA's record-keeping guidance, and a badge asserting otherwise on an
 * export an accountant reads is the one thing this software must not do.
 * Coverage is countable; compliance is somebody's judgement.
 */
export function DocumentCoverageBadge({ className, matchRate }: DocumentCoverageBadgeProps) {
  if (matchRate !== undefined && matchRate < WELL_DOCUMENTED_THRESHOLD) return null;
  return (
    <Badge className={`bg-success/10 text-success border-success/20 ${className ?? ""}`}>
      <FileCheck className="h-3 w-3 mr-1" />
      {matchRate === undefined ? "Documented" : `${Math.round(matchRate)}% documented`}
    </Badge>
  );
}
