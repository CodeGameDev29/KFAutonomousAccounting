import { useState } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Info,
  CheckCircle2,
  Check,
  Sparkles,
  Loader2,
  Building2,
} from "lucide-react";
import {
  useReasonablenessFlags,
  useDismissFlag,
} from "@/hooks/use-reasonableness-flags";
import { formatSignedMoney } from "@/lib/money";
import { attentionOf, directionLabel } from "@/lib/reasonableness-status";
import { PercentileStrip } from "./PercentileStrip";
import { ProfitabilityStrip } from "./ProfitabilityStrip";
import { RecommendationLine } from "./RecommendationLine";
import type {
  ReasonablenessBand,
  ReasonablenessComparison,
  ReasonablenessFlag,
} from "@/types/reasonableness";

// The only summary verdicts this card may state, fixed here on purpose: the
// wording is the legal framing, and nothing may generate a new one.
const BAND_LABELS: Record<ReasonablenessBand, string> = {
  typical: "Looks typical",
  few: "A few things worth a look",
  several: "Several things stand out — worth reviewing with your bookkeeper",
};
// Mandatory disclaimer — always visible, not behind a tooltip.
const DISCLAIMER =
  "Peer comparison, not tax advice or an audit prediction — compares to public averages only.";
const OGL_URL = "https://open.canada.ca/en/open-government-licence-canada";

function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

// Per-row "both-readings" defuser for a worth-a-look category: a row that
// flags a category must pair its position with the innocent reading of the
// same number, so a high line never reads as an accusation.
// Only used when the backend supplies no deterministic `recommendation` prose.
function worthALookDefuser(c: ReasonablenessComparison): string {
  if (c.direction === "high")
    return "If this is core to how you work, you're all set — this is just a heads-up.";
  if (c.direction === "low")
    return "Worth a quick check that you're not missing a deduction here.";
  return "Just a heads-up — worth a quick look with your bookkeeper.";
}

function BandPill({ band }: { band: ReasonablenessBand }) {
  // Blue/grey only — no green/amber valence, drop the alarm triangle.
  const cfg = {
    typical: {
      cls: "border-primary/30 bg-primary/5 text-primary",
      Icon: CheckCircle2,
    },
    few: {
      cls: "border-border bg-muted text-muted-foreground",
      Icon: Info,
    },
    several: {
      cls: "border-primary/40 bg-primary/10 text-primary",
      Icon: Info,
    },
  }[band];
  return (
    <Badge variant="outline" className={`gap-1.5 ${cfg.cls}`}>
      <cfg.Icon className="h-3.5 w-3.5" />
      {BAND_LABELS[band]}
    </Badge>
  );
}

function ComparisonRow({
  c,
  onDismiss,
  dismissing,
}: {
  c: ReasonablenessComparison;
  onDismiss: (id: string) => void;
  dismissing: boolean;
}) {
  const attention = attentionOf(c.status);
  const hasBenchmark = c.status !== "no_benchmark" && c.peer_pct !== null;
  const isWorthALook = attention === "worth_a_look" && !c.dismissed;
  const noBenchmarkCaption =
    c.status === "no_benchmark"
      ? c.unavailable_reason === "insufficient_filings"
        ? "Not enough Canadian filings to show a reliable range."
        : "No separate industry figure — grouped into “other” by Statistics Canada."
      : null;

  return (
    <div className="flex flex-col gap-1.5 border-t border-border/60 py-2.5 first:border-t-0">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-sm font-medium text-foreground">{c.label}</span>
          {c.cra_scrutinized && (
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Commonly reviewed category
            </span>
          )}
          {attention === "worth_a_look" && (
            <Info className="h-3.5 w-3.5 flex-shrink-0 text-primary" />
          )}
        </div>
        <div className="flex items-center gap-2 sm:flex-shrink-0">
          <Badge
            variant="outline"
            className="whitespace-nowrap border-border bg-muted/40 text-muted-foreground"
          >
            {directionLabel(c.status)}
          </Badge>
          {isWorthALook && c.flag_key && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs text-muted-foreground hover:text-foreground"
              disabled={dismissing}
              onClick={() => onDismiss(c.flag_key as string)}
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
      </div>

      <PercentileStrip
        label={c.label}
        yourPct={c.your_pct}
        peerPct={c.peer_pct}
        peerBand={c.peer_band}
        attention={attention}
        directionText={directionLabel(c.status)}
        hasBenchmark={hasBenchmark}
      />

      <span className="text-xs text-muted-foreground">
        You: {pct(c.your_pct)}
        {c.peer_pct !== null ? ` · Industry average: ${pct(c.peer_pct)}` : ""}{" "}
        &middot; {formatSignedMoney(c.amount, c.currency)}
      </span>

      {noBenchmarkCaption && (
        <span className="text-xs text-muted-foreground">{noBenchmarkCaption}</span>
      )}

      {c.recommendation ? (
        <RecommendationLine text={c.recommendation} />
      ) : (
        isWorthALook && (
          <p className="text-xs text-muted-foreground">{worthALookDefuser(c)}</p>
        )
      )}
    </div>
  );
}

function PatternNote({
  flag,
  onDismiss,
  dismissing,
}: {
  flag: ReasonablenessFlag;
  onDismiss: (id: string) => void;
  dismissing: boolean;
}) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2">
      <Info className="mt-0.5 h-4 w-4 flex-shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-foreground">{flag.title}</p>
        <p className="mt-0.5 text-sm text-muted-foreground">{flag.prompt}</p>
        {flag.recommendation && (
          <p className="mt-1 text-sm text-muted-foreground">
            {flag.recommendation}
          </p>
        )}
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 flex-shrink-0 text-xs text-muted-foreground hover:text-foreground"
        disabled={dismissing}
        onClick={() => onDismiss(flag.id)}
      >
        {dismissing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Expected"}
      </Button>
    </div>
  );
}

function Reassurance({ text }: { text: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg bg-muted/50 px-3 py-2">
      <Sparkles className="mt-0.5 h-4 w-4 flex-shrink-0 text-primary" />
      <p className="text-sm text-foreground">
        <span className="font-medium">Good sign: </span>
        {text}
      </p>
    </div>
  );
}

function ComparisonSource({
  meta,
}: {
  meta: import("@/types/reasonableness").ReasonablenessMeta;
}) {
  const naics = meta.naics_code;
  const norms = [
    meta.pct_profitable != null
      ? `${Math.round(meta.pct_profitable * 100)}% are profitable`
      : null,
    meta.gross_margin != null
      ? `gross margin ~${(meta.gross_margin * 100).toFixed(0)}%`
      : null,
    meta.net_profit_margin != null
      ? `net margin ~${(meta.net_profit_margin * 100).toFixed(1)}%`
      : null,
  ].filter(Boolean);
  return (
    <details open className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
      <summary className="cursor-pointer font-medium text-foreground">
        What you're compared to
      </summary>
      <ul className="mt-2 space-y-1">
        <li>
          <span className="text-foreground">Industry:</span>{" "}
          {meta.cohort_label ?? meta.industry_label ?? "—"}
          {naics ? ` (NAICS ${naics})` : ""}
          {meta.naics_source === "data_inferred"
            ? " · inferred from your spending"
            : meta.naics_source === "industry_map"
              ? " · from your saved industry"
              : ""}
        </li>
        {meta.revenue_band_label && (
          <li>
            <span className="text-foreground">Business size:</span> Canadian
            businesses with {meta.revenue_band_label}
            {meta.annualized_revenue != null
              ? ` — your revenue annualizes to ≈${formatSignedMoney(
                  meta.annualized_revenue,
                  meta.currency,
                )}, which lands in this band`
              : ""}
          </li>
        )}
        <li>
          <span className="text-foreground">Location:</span>{" "}
          {meta.cohort_province
            ? `${meta.cohort_province} businesses (provincial figures)`
            : `Canada (national)${
                meta.province
                  ? ` — you're in ${meta.province}; these are national figures, not province-specific`
                  : ""
              }`}
        </li>
        <li>
          <span className="text-foreground">Based on:</span>{" "}
          {meta.sample_size != null
            ? `≈${meta.sample_size.toLocaleString()} businesses' `
            : ""}
          {meta.benchmark_year ?? ""} CRA tax filings via ISED / Statistics Canada
        </li>
        {norms.length > 0 && (
          <li>
            <span className="text-foreground">Industry norms:</span>{" "}
            {norms.join(" · ")}
          </li>
        )}
        <li className="italic">
          ISED publishes this at the industry-group level.
        </li>
        {(meta.source_url || meta.source_tool_url || meta.source_dataset_url) && (
          <li className="pt-1">
            <span className="text-foreground">Verify:</span>{" "}
            {meta.source_url && (
              <a
                href={meta.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline hover:text-foreground"
              >
                this industry's data
              </a>
            )}
            {meta.source_url && meta.source_dataset_url ? " · " : ""}
            {meta.source_dataset_url && (
              <a
                href={meta.source_dataset_url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline hover:text-foreground"
              >
                full dataset
              </a>
            )}
            {(meta.source_url || meta.source_dataset_url) && meta.source_tool_url
              ? " · "
              : ""}
            {meta.source_tool_url && (
              <a
                href={meta.source_tool_url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline hover:text-foreground"
              >
                ISED tool
              </a>
            )}
          </li>
        )}
      </ul>
    </details>
  );
}

function AttributionFooter({ attribution }: { attribution: string }) {
  return (
    <p className="border-t border-border/60 pt-3 text-[11px] leading-relaxed text-muted-foreground">
      {attribution}{" "}
      <a
        href={OGL_URL}
        target="_blank"
        rel="noopener noreferrer"
        className="underline hover:text-foreground"
      >
        Open Government Licence – Canada
      </a>
      .
    </p>
  );
}

function CardShell({ children }: { children: React.ReactNode }) {
  return (
    <Card className="w-full rounded-xl shadow-warm">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Building2 className="h-5 w-5 text-primary" />
          <CardTitle className="text-lg font-bold">
            How you compare to similar businesses
          </CardTitle>
        </div>
        <CardDescription>
          Each category as a share of revenue vs the public average for similar
          Canadian businesses.
        </CardDescription>
      </CardHeader>
      {children}
    </Card>
  );
}

export function ReasonablenessCard({
  start,
  end,
}: {
  start: string | null;
  end: string | null;
}) {
  const { data, isLoading, isFetching, error, refetch } =
    useReasonablenessFlags(start, end);
  const dismiss = useDismissFlag(start, end);
  const [dismissingId, setDismissingId] = useState<string | null>(null);

  const handleDismiss = (id: string) => {
    setDismissingId(id);
    dismiss.mutate(
      { id, dismissed: true },
      { onSettled: () => setDismissingId(null) },
    );
  };

  const handleRestoreAll = () => {
    const keys = data?.meta.dismissed_flag_keys ?? [];
    keys.forEach((id) => dismiss.mutate({ id, dismissed: false }));
  };

  if (error) {
    return (
      <CardShell>
        <CardContent>
          <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4">
            <p className="text-sm font-medium text-foreground">
              Comparison unavailable right now.
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              This doesn't mean anything is wrong with your books — try again in a
              moment.
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => refetch()}
            >
              Retry
            </Button>
          </div>
        </CardContent>
      </CardShell>
    );
  }

  if (isLoading || !data) {
    return (
      <CardShell>
        <CardContent className="space-y-3">
          <Skeleton className="h-7 w-48 rounded-full" />
          <Skeleton className="h-16 w-full rounded-lg" />
          <Skeleton className="h-16 w-full rounded-lg" />
        </CardContent>
      </CardShell>
    );
  }

  // Hide entirely when there's nothing (or no matching industry cohort) to compare.
  if (data.status === "no_data" || data.status === "missing_naics") return null;

  if (data.status === "below_floor") {
    return (
      <CardShell>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Not enough revenue in this period to benchmark reliably — pick a
            longer period or check back once there's more activity.
          </p>
          <AttributionFooter attribution={data.meta.attribution} />
        </CardContent>
      </CardShell>
    );
  }

  // status === "ok"
  const { band, comparisons, flags, reassurances, meta } = data;
  // travel/meals/vehicle over-threshold appear as "watch" rows in the
  // comparison table, so their pattern notes would be redundant.
  const patternFlags = flags.filter(
    (f) => f.id.startsWith("pattern_") && !f.id.endsWith("_over_threshold"),
  );
  const infoFlags = flags.filter((f) => f.id.startsWith("info_"));
  const dismissedKeys = meta.dismissed_flag_keys ?? [];

  // Layer (a) — reassurance headline computed client-side.
  const inBand = comparisons.filter(
    (c) => attentionOf(c.status) === "calm" && c.status !== "no_benchmark",
  ).length;
  const benchmarked = comparisons.filter(
    (c) => c.status !== "no_benchmark",
  ).length;
  const headline =
    benchmarked === 0
      ? "There aren't enough Canadian filings in the benchmark set to compare your categories yet."
      : `${inBand} of ${benchmarked} categories are in the typical range.`;

  // Layer (b) — "Worth a look", ranked by weight desc, top 4.
  const worthALook = comparisons
    .filter((c) => attentionOf(c.status) === "worth_a_look" && !c.dismissed)
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 4);

  const rowDismissing = (c: ReasonablenessComparison) =>
    c.flag_key !== null && dismissingId === c.flag_key;

  return (
    <CardShell>
      <CardContent
        className={
          isFetching
            ? "space-y-4 opacity-60 transition-opacity"
            : "space-y-4 transition-opacity"
        }
      >
        {/* Layer (a) — reassurance header (always) */}
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {band && <BandPill band={band} />}
            {meta.naics_source === "data_inferred" && (
              <span className="text-xs text-muted-foreground">
                Industry inferred from your spending
              </span>
            )}
            {meta.partial_year && (
              <span className="text-xs text-muted-foreground">
                Indicative — partial year ({meta.period_months} months)
              </span>
            )}
          </div>
          <p className="text-sm font-medium text-foreground">{headline}</p>
          <div>
            <Badge
              variant="outline"
              className="border-border bg-transparent text-[11px] font-normal text-muted-foreground"
            >
              {meta.business_count != null
                ? `Compared to ~${meta.business_count.toLocaleString()} similar Canadian businesses' tax filings · ${meta.benchmark_year}.`
                : `Compared to similar Canadian businesses' tax filings · ${meta.benchmark_year}.`}
            </Badge>
          </div>
        </div>

        <ComparisonSource meta={meta} />

        {reassurances.map((r, i) => (
          <Reassurance key={`r-${i}`} text={r} />
        ))}
        {infoFlags.map((f) => (
          <Reassurance key={f.id} text={f.prompt} />
        ))}

        {comparisons.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <CheckCircle2 className="h-4 w-4 text-primary" />
            Nothing to compare against similar businesses for this period.
          </p>
        ) : (
          <>
            {/* Layer (b) — Worth a look (default open) */}
            {worthALook.length > 0 ? (
              <div className="space-y-1">
                <h4 className="text-sm font-semibold text-foreground">
                  Worth a look
                </h4>
                <div className="rounded-lg border border-border/60 px-3">
                  {worthALook.map((c) => (
                    <ComparisonRow
                      key={c.gifi_bucket}
                      c={c}
                      dismissing={rowDismissing(c)}
                      onDismiss={handleDismiss}
                    />
                  ))}
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Nothing stands out — every category with a benchmark is in the
                typical range.
              </p>
            )}

            {/* Layer (c) — Show all categories (collapsed) */}
            <details className="rounded-lg border border-border/60 px-3">
              <summary className="cursor-pointer py-2.5 text-sm text-muted-foreground">
                Show all {comparisons.length} categories
              </summary>
              <div className="pb-1">
                {comparisons.map((c) => (
                  <ComparisonRow
                    key={c.gifi_bucket}
                    c={c}
                    dismissing={rowDismissing(c)}
                    onDismiss={handleDismiss}
                  />
                ))}
              </div>
            </details>
          </>
        )}

        {/* Always-on profitability section */}
        {data.profitability?.length ? (
          <div className="border-t border-border/60 pt-4">
            <ProfitabilityStrip
              lines={data.profitability}
              cohortLabel={data.profitability_cohort_label ?? ""}
              currency={meta.currency}
            />
          </div>
        ) : null}

        {/* Cross-cutting pattern insights (contractor mix, fragmentation, etc.) */}
        {patternFlags.map((f) => (
          <PatternNote
            key={f.id}
            flag={f}
            dismissing={dismissingId === f.id}
            onDismiss={handleDismiss}
          />
        ))}

        {dismissedKeys.length > 0 && (
          <button
            type="button"
            onClick={handleRestoreAll}
            disabled={dismiss.isPending}
            className="text-xs text-muted-foreground underline hover:text-foreground disabled:opacity-50"
          >
            Restore {dismissedKeys.length} item
            {dismissedKeys.length > 1 ? "s" : ""} you marked as expected
          </button>
        )}

        <p className="text-xs text-muted-foreground">{DISCLAIMER}</p>
        <AttributionFooter attribution={meta.attribution} />
      </CardContent>
    </CardShell>
  );
}
