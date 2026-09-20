interface CoverageIndicatorProps {
  coveredAmount: number;
  totalAmount: number;
  currency: string;
  /** null = not computable (all valued proofs are in a different currency). */
  coveragePct: number | null;
  /** Proofs whose currency differs from the transaction (excluded from the ratio). */
  crossCurrencyCount?: number;
  className?: string;
  compact?: boolean;
}

export function CoverageIndicator({
  coveredAmount,
  totalAmount,
  currency,
  coveragePct,
  crossCurrencyCount = 0,
  className = "",
  compact = false,
}: CoverageIndicatorProps) {
  // Cross-currency proofs (e.g. a US$1,000 invoice matched to a C$1,380
  // charge) can't be compared without FX conversion — never show a falsely
  // low percentage or a red "Partial" chip for a correct FX match.
  if (coveragePct === null) {
    return (
      <div className={`space-y-1 ${className}`}>
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            Receipt{crossCurrencyCount === 1 ? "" : "s"} in a different
            currency — amount coverage not comparable without conversion
          </span>
          <span className="inline-block rounded px-1.5 py-0.5 text-[10px] font-medium text-sky-700 bg-sky-100">
            FX match
          </span>
        </div>
      </div>
    );
  }

  const pct = Math.min(coveragePct * 100, 100);
  const color =
    pct >= 95
      ? "bg-green-500"
      : pct >= 80
        ? "bg-amber-500"
        : "bg-red-500";
  const chipLabel =
    pct >= 95 ? "Complete" : pct >= 80 ? "Gap Warning" : "Partial";
  const chipColor =
    pct >= 95
      ? "text-green-700 bg-green-100"
      : pct >= 80
        ? "text-amber-700 bg-amber-100"
        : "text-red-700 bg-red-100";

  const prefix =
    currency === "CAD" ? "C$" : currency === "USD" ? "US$" : "";
  const suffix = prefix ? "" : ` ${currency}`;

  return (
    <div className={`space-y-1 ${className}`}>
      {/* Progress bar */}
      <div
        className="h-2 w-full rounded-full bg-muted/30"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
      >
        <div
          className={`h-full rounded-full transition-all duration-300 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {/* Label row */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        {!compact && (
          <span>
            {prefix}
            {coveredAmount.toFixed(2)}
            {suffix} of {prefix}
            {totalAmount.toFixed(2)}
            {suffix} ({Math.round(pct)}%)
            {crossCurrencyCount > 0 &&
              ` · +${crossCurrencyCount} receipt${crossCurrencyCount === 1 ? "" : "s"} in another currency`}
          </span>
        )}
        {compact && <span>{Math.round(pct)}% covered</span>}
        <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-medium ${chipColor}`}>
          {chipLabel}
        </span>
      </div>
    </div>
  );
}
