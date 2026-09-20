import type { Attention } from "@/lib/reasonableness-status";

// Bullet graph on a real %-of-revenue value axis (auto-scaled per
// row) — NOT a 0-100 percentile axis. Grey band spans peer_band, a tick marks
// peer_pct, the bold "You" dot sits at the user's real your_pct.
export interface PercentileStripProps {
  label: string;
  yourPct: number;
  peerPct: number | null;
  peerBand: [number, number] | null;
  attention: Attention;
  directionText: string;
  hasBenchmark: boolean;
}

function pctAria(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

function clamp(v: number): number {
  return Math.min(98, Math.max(2, v));
}

export function PercentileStrip({
  label,
  yourPct,
  peerPct,
  peerBand,
  attention,
  directionText,
  hasBenchmark,
}: PercentileStripProps) {
  const noBenchmark = !hasBenchmark || attention === "no_benchmark";

  if (noBenchmark) {
    const ariaLabel = `${label}: about ${pctAria(yourPct)} of revenue. No separate industry figure.`;
    return (
      <div className="w-full">
        <div className="relative h-2 w-full rounded-full border border-dashed border-border bg-transparent">
          <div
            role="img"
            aria-label={ariaLabel}
            tabIndex={0}
            className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-muted-foreground/50 ring-2 ring-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            style={{ left: "50%" }}
          />
        </div>
        <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
          <span>less</span>
          <span>more</span>
        </div>
      </div>
    );
  }

  // Auto-scale the axis per row so every mark stays on-track.
  const domainMax =
    Math.max(yourPct, peerPct ?? 0, peerBand?.[1] ?? 0) * 1.25 || 0.01;
  const pos = (v: number) => clamp((v / domainMax) * 100);

  const dotCls =
    attention === "worth_a_look"
      ? "h-4 w-4 bg-primary shadow-sm"
      : "h-3 w-3 bg-muted-foreground";

  const ariaLabel =
    peerPct !== null
      ? `${label}: your spending is ${directionText} — about ${pctAria(yourPct)} of revenue versus about ${pctAria(peerPct)} for the typical similar business.`
      : `${label}: your spending is ${directionText} — about ${pctAria(yourPct)} of revenue.`;

  return (
    <div className="w-full">
      <div className="relative h-2 w-full rounded-full bg-muted/40">
        {peerBand && (
          <div
            className="absolute top-1/2 h-2 -translate-y-1/2 rounded-full bg-muted"
            style={{
              left: `${pos(peerBand[0])}%`,
              width: `${Math.max(0, pos(peerBand[1]) - pos(peerBand[0]))}%`,
            }}
          />
        )}
        {peerPct !== null && (
          <div
            className="absolute top-1/2 h-3 w-0.5 -translate-x-1/2 -translate-y-1/2 bg-border"
            style={{ left: `${pos(peerPct)}%` }}
          />
        )}
        <div
          role="img"
          aria-label={ariaLabel}
          tabIndex={0}
          className={`absolute top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 ${dotCls}`}
          style={{ left: `${pos(yourPct)}%` }}
        />
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
        <span>less</span>
        <span>more</span>
      </div>
    </div>
  );
}
