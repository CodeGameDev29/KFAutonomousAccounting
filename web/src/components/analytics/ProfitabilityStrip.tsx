import type { ProfitabilityLine } from "@/types/reasonableness";

// Always-on peer-cost section. Descriptive only: it reports where peers sit
// and never ranks, scores or judges the user — no status, no valence, no red
// state, calm blue/grey only.
export interface ProfitabilityStripProps {
  lines: ProfitabilityLine[];
  cohortLabel: string;
  currency: string;
}

function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

function clamp(v: number): number {
  return Math.min(98, Math.max(2, v));
}

export function ProfitabilityStrip({ lines }: ProfitabilityStripProps) {
  if (!lines?.length) return null;

  const ordered = [...lines].sort((a, b) => b.your_pct - a.your_pct).slice(0, 6);

  return (
    <div className="space-y-3">
      <div className="space-y-0.5">
        <h4 className="text-sm font-semibold text-foreground">
          How your costs compare to the most-profitable similar businesses
        </h4>
        <p className="text-xs text-muted-foreground">
          Descriptive comparison to the most-profitable quarter of similar
          Canadian businesses — a reference point, not a target.
        </p>
      </div>

      {/* Legend — name the two markers so they don't read as stray icons. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rotate-45 bg-primary" aria-hidden />
          most-profitable peers
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-full bg-muted-foreground" aria-hidden />
          you
        </span>
      </div>

      <div className="space-y-2.5">
        {ordered.map((line) => {
          const domainMax =
            Math.max(line.your_pct, line.peer_pct) * 1.25 || 0.01;
          const peerLeft = clamp((line.peer_pct / domainMax) * 100);
          const youLeft = clamp((line.your_pct / domainMax) * 100);
          const aria = `${line.label}: the most-profitable quarter of similar businesses spend about ${pct(
            line.peer_pct,
          )} of revenue here; you spend about ${pct(line.your_pct)}.`;
          return (
            <div key={line.gifi_bucket} className="space-y-1">
              <p className="text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{line.label}</span>{" "}
                — top-profit peers spend about {pct(line.peer_pct)}; you spend{" "}
                {pct(line.your_pct)}.
              </p>
              <div
                role="img"
                aria-label={aria}
                className="relative h-2 w-full max-w-md rounded-full bg-muted"
              >
                <div
                  className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rotate-45 bg-primary ring-2 ring-background"
                  style={{ left: `${peerLeft}%` }}
                />
                <div
                  className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-muted-foreground ring-2 ring-background"
                  style={{ left: `${youLeft}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>

      <p className="text-xs text-muted-foreground">
        Many healthy businesses spend differently and do just fine — this is
        context for a chat with your bookkeeper, not a verdict.
      </p>
    </div>
  );
}
