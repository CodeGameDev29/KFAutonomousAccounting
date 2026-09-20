import { useMemo } from "react";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { formatSignedMoney } from "@/lib/money";
import { useFxRates } from "@/hooks/use-fx-rates";
import type { CategoryBreakdown } from "@/types/analytics";

interface CurrencyTotals {
  currency: string;
  moneyIn: number;
  moneyOut: number;
  net: number;
}

function totalsByCurrency(categories: CategoryBreakdown[]): CurrencyTotals[] {
  const map = new Map<string, CurrencyTotals>();
  for (const c of categories) {
    const t = map.get(c.currency) ?? {
      currency: c.currency,
      moneyIn: 0,
      moneyOut: 0,
      net: 0,
    };
    t.moneyIn += c.income;
    t.moneyOut += c.expenses;
    t.net += c.net;
    map.set(c.currency, t);
  }
  // CAD first, then by activity volume so the user's main money is on top.
  return [...map.values()].sort((a, b) => {
    if (a.currency === "CAD" && b.currency !== "CAD") return -1;
    if (b.currency === "CAD" && a.currency !== "CAD") return 1;
    return (
      Math.abs(b.moneyIn) + Math.abs(b.moneyOut) -
      (Math.abs(a.moneyIn) + Math.abs(a.moneyOut))
    );
  });
}

const formatMoney = formatSignedMoney;

interface DeltaProps {
  current: number;
  /** undefined = comparison unknown (loading/failed); null = known no activity. */
  previous: number | null | undefined;
  currency: string;
  compareLabel: string;
  /** Is an increase good news (income) or bad news (spending)? */
  moreIsGood: boolean;
  approx?: boolean;
}

function Delta({ current, previous, currency, compareLabel, moreIsGood, approx }: DeltaProps) {
  if (previous === undefined) return null;
  if (previous === null) {
    return (
      <p className="text-xs text-muted-foreground mt-1">
        No activity in {compareLabel}
      </p>
    );
  }
  const delta = current - previous;
  if (Math.abs(delta) < 0.005) {
    return (
      <p className="flex items-center gap-1 text-xs text-muted-foreground mt-1">
        <Minus className="h-3 w-3" />
        same as {compareLabel}
      </p>
    );
  }
  const up = delta > 0;
  const good = up === moreIsGood;
  const Icon = up ? ArrowUpRight : ArrowDownRight;
  return (
    <p
      className={cn(
        "flex items-center gap-1 text-xs font-medium mt-1",
        good ? "text-success" : "text-accent",
      )}
    >
      <Icon className="h-3 w-3" />
      {approx ? "≈" : ""}
      {formatMoney(Math.abs(delta), currency)} {up ? "more" : "less"} than{" "}
      {compareLabel}
    </p>
  );
}

interface TilePrev {
  moneyIn: number | null;
  moneyOut: number | null;
  net: number | null;
}

interface SummaryTilesProps {
  label?: string;
  caption?: string;
  currency: string;
  suffix?: string; // e.g. " (CAD)" on the tile titles
  moneyIn: number;
  moneyOut: number;
  net: number;
  prev: TilePrev | undefined;
  compareLabel: string;
  approx?: boolean;
}

function SummaryTiles({
  label,
  caption,
  currency,
  suffix = "",
  moneyIn,
  moneyOut,
  net,
  prev,
  compareLabel,
  approx = false,
}: SummaryTilesProps) {
  const pre = approx ? "≈" : "";
  return (
    <div className="space-y-2">
      {label && (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
          <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {label}
          </p>
          {caption && (
            <p className="text-[11px] text-muted-foreground">{caption}</p>
          )}
        </div>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Card className="shadow-warm-sm bg-white border-l-[3px] border-l-success">
          <CardContent className="p-5">
            <p className="text-sm font-medium text-muted-foreground">
              Money in{suffix}
            </p>
            <p className="font-mono text-3xl font-bold tracking-tight text-foreground mt-1">
              {pre}
              {formatMoney(moneyIn, currency)}
            </p>
            <Delta
              current={moneyIn}
              previous={prev?.moneyIn}
              currency={currency}
              compareLabel={compareLabel}
              moreIsGood
              approx={approx}
            />
          </CardContent>
        </Card>
        <Card className="shadow-warm-sm bg-white border-l-[3px] border-l-accent">
          <CardContent className="p-5">
            <p className="text-sm font-medium text-muted-foreground">
              Money out{suffix}
            </p>
            <p className="font-mono text-3xl font-bold tracking-tight text-foreground mt-1">
              {pre}
              {formatMoney(moneyOut, currency)}
            </p>
            <Delta
              current={moneyOut}
              previous={prev?.moneyOut}
              currency={currency}
              compareLabel={compareLabel}
              moreIsGood={false}
              approx={approx}
            />
          </CardContent>
        </Card>
        <Card
          className={cn(
            "shadow-warm-sm bg-white border-l-[3px]",
            net >= 0 ? "border-l-success" : "border-l-destructive",
          )}
        >
          <CardContent className="p-5">
            <p className="text-sm font-medium text-muted-foreground">
              {net >= 0 ? "Net profit" : "Net loss"}
              {suffix}
            </p>
            <p
              className={cn(
                "font-mono text-3xl font-bold tracking-tight mt-1",
                net >= 0 ? "text-success" : "text-destructive",
              )}
            >
              {pre}
              {formatMoney(net, currency)}
            </p>
            <Delta
              current={net}
              previous={prev?.net}
              currency={currency}
              compareLabel={compareLabel}
              moreIsGood
              approx={approx}
            />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

interface AnalyticsSummaryProps {
  categories: CategoryBreakdown[];
  prevCategories: CategoryBreakdown[] | undefined;
  compareLabel: string;
  periodLabel: string;
  isLoading: boolean;
}

export function AnalyticsSummary({
  categories,
  prevCategories,
  compareLabel,
  periodLabel,
  isLoading,
}: AnalyticsSummaryProps) {
  const totals = useMemo(() => totalsByCurrency(categories), [categories]);
  const prevTotals = useMemo(
    () => (prevCategories ? totalsByCurrency(prevCategories) : null),
    [prevCategories],
  );

  const currenciesInPlay = useMemo(
    () => [
      ...new Set([
        ...totals.map((t) => t.currency),
        ...(prevTotals ?? []).map((t) => t.currency),
      ]),
    ],
    [totals, prevTotals],
  );
  const fx = useFxRates(currenciesInPlay);

  // Convert a set of per-currency totals into approximate CAD, or null when
  // any needed rate is unavailable (never show a knowingly wrong total).
  const toCad = (list: CurrencyTotals[]): CurrencyTotals | null => {
    const sum: CurrencyTotals = { currency: "CAD", moneyIn: 0, moneyOut: 0, net: 0 };
    for (const t of list) {
      const rate =
        t.currency === "CAD" ? 1 : fx.data?.rates[t.currency]?.rate;
      if (rate === undefined) return null;
      sum.moneyIn += t.moneyIn * rate;
      sum.moneyOut += t.moneyOut * rate;
      sum.net += t.net * rate;
    }
    return sum;
  };

  if (isLoading) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-[110px] rounded-xl" />
        ))}
      </div>
    );
  }

  if (totals.length === 0) return null;

  const multiCurrency = totals.length > 1;
  const combined = multiCurrency ? toCad(totals) : null;
  const combinedPrev =
    combined && prevTotals !== null ? toCad(prevTotals) : null;

  const rateCaption = combined
    ? Object.entries(fx.data?.rates ?? {})
        .filter(([c]) => currenciesInPlay.includes(c))
        .map(
          ([c, r]) =>
            `1 ${c} ≈ ${r.rate.toFixed(4)} CAD (Bank of Canada, ${r.as_of})`,
        )
        .join(" · ")
    : undefined;

  const headlineSource = combined ?? totals[0];
  const pre = combined ? "≈" : "";
  const headline = `In ${periodLabel}, you brought in ${pre}${formatMoney(headlineSource.moneyIn, headlineSource.currency)} and spent ${pre}${formatMoney(headlineSource.moneyOut, headlineSource.currency)} — net ${pre}${formatMoney(headlineSource.net, headlineSource.currency)}${combined ? " across all accounts" : multiCurrency ? ` (${totals[0].currency})` : ""}.`;

  const prevFor = (t: CurrencyTotals): TilePrev | undefined => {
    if (prevTotals === null) return undefined;
    const match = prevTotals.find((p) => p.currency === t.currency);
    return {
      moneyIn: match?.moneyIn ?? null,
      moneyOut: match?.moneyOut ?? null,
      net: match?.net ?? null,
    };
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-foreground" data-testid="analytics-headline">
        {headline}
      </p>

      {combined && (
        <SummaryTiles
          label="All accounts (converted to CAD)"
          caption={rateCaption}
          currency="CAD"
          suffix=" (≈ CAD)"
          moneyIn={combined.moneyIn}
          moneyOut={combined.moneyOut}
          net={combined.net}
          prev={
            combinedPrev
              ? {
                  moneyIn: combinedPrev.moneyIn,
                  moneyOut: combinedPrev.moneyOut,
                  net: combinedPrev.net,
                }
              : undefined
          }
          compareLabel={compareLabel}
          approx
        />
      )}

      {multiCurrency && !combined && !fx.isLoading && (
        <p className="text-xs text-muted-foreground">
          Couldn't fetch today's exchange rate, so totals are shown per
          currency below.
        </p>
      )}

      {totals.map((t) => (
        <SummaryTiles
          key={t.currency}
          label={multiCurrency ? `${t.currency} accounts` : undefined}
          currency={t.currency}
          suffix={multiCurrency ? ` (${t.currency})` : ""}
          moneyIn={t.moneyIn}
          moneyOut={t.moneyOut}
          net={t.net}
          prev={prevFor(t)}
          compareLabel={compareLabel}
        />
      ))}
    </div>
  );
}
