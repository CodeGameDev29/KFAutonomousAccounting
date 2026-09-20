import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { CHART_AMBER, CHART_GREEN } from "@/lib/money";
import { useFxRates } from "@/hooks/use-fx-rates";

interface MonthlyTrendEntry {
  month: number;
  currency: string;
  income: number;
  expenses: number;
}

interface MonthlyTrendResponse {
  year: number;
  entries: MonthlyTrendEntry[];
}

const MONTH_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatCompactAxis(value: number): string {
  if (value >= 1000) return `$${(value / 1000).toFixed(0)}K`;
  return `$${value.toFixed(0)}`;
}

interface ChartDatum {
  month: number;
  label: string;
  "Money in": number;
  "Money out": number;
}

interface CustomTooltipProps {
  active?: boolean;
  payload?: Array<{ value: number; name: string; color: string }>;
  label?: string;
  approx?: boolean;
}

function CustomTooltip({ active, payload, label, approx }: CustomTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-lg border bg-white px-3 py-2 shadow-warm-md text-sm">
      <p className="font-semibold text-foreground mb-1">{label}</p>
      {payload.map((entry, i) => (
        <p key={i} className="text-xs text-foreground">
          <span
            className="inline-block h-2 w-2 rounded-full mr-1.5"
            style={{ backgroundColor: entry.color }}
          />
          {entry.name}: {approx ? "≈" : ""}$
          {entry.value.toLocaleString("en-CA", {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0,
          })}
        </p>
      ))}
      <p className="text-[11px] text-muted-foreground mt-1">
        Click to view this month
      </p>
    </div>
  );
}

interface MonthlyTrendChartProps {
  year: number;
  /** 1-12 when a single calendar month is selected; null for custom ranges. */
  selectedMonth: number | null;
  onSelectMonth: (year: number, month: number) => void;
}

export function MonthlyTrendChart({
  year,
  selectedMonth,
  onSelectMonth,
}: MonthlyTrendChartProps) {
  const { data, isLoading } = useQuery<MonthlyTrendResponse>({
    queryKey: ["analytics", "monthly-trend", year],
    queryFn: () => api.get(`/api/analytics/monthly-trend?year=${year}`),
    staleTime: 5 * 60 * 1000,
  });

  const entries = data?.entries ?? [];
  const currencies = [...new Set(entries.map((e) => e.currency))];
  const nonCad = currencies.filter((c) => c !== "CAD");
  const fx = useFxRates(currencies);

  const rateFor = (currency: string): number | undefined =>
    currency === "CAD" ? 1 : fx.data?.rates[currency]?.rate;

  // A currency with no rate is left out of the bars (never mixed at par) and
  // called out in the caption instead.
  const unconverted = nonCad.filter((c) => rateFor(c) === undefined);
  const converting = nonCad.length > 0 && unconverted.length < nonCad.length;

  const chartData: ChartDatum[] = Array.from({ length: 12 }, (_, i) => {
    const month = i + 1;
    let moneyIn = 0;
    let moneyOut = 0;
    for (const e of entries) {
      if (e.month !== month) continue;
      const rate = rateFor(e.currency);
      if (rate === undefined) continue;
      moneyIn += e.income * rate;
      moneyOut += e.expenses * rate;
    }
    return {
      month,
      label: MONTH_SHORT[i],
      "Money in": Math.round(moneyIn),
      "Money out": Math.round(moneyOut),
    };
  });

  const hasAny = entries.length > 0;
  const approx = nonCad.length > 0;

  const caption = [
    nonCad.length > 0
      ? "All accounts converted to CAD at the Bank of Canada daily rate."
      : "All accounts.",
    "Transfers between your own accounts are excluded.",
    unconverted.length > 0
      ? `No rate available for ${unconverted.join(", ")} — those amounts are not shown.`
      : "",
    "Click a month to see its breakdown.",
  ]
    .filter(Boolean)
    .join(" ");

  // Recharts click payloads have carried the datum both at the top level and
  // nested under .payload across versions — accept either, validate the month.
  const handleClick = (payload: unknown) => {
    const p = payload as
      | { month?: unknown; payload?: { month?: unknown } }
      | undefined;
    const m = typeof p?.month === "number" ? p.month : p?.payload?.month;
    if (typeof m === "number" && m >= 1 && m <= 12) onSelectMonth(year, m);
  };

  const dimmed = (month: number) =>
    selectedMonth !== null && month !== selectedMonth;

  const waitingOnRates = hasAny && nonCad.length > 0 && fx.isLoading;

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader className="pb-2">
        <CardTitle className="text-lg font-bold">
          Month by month · {year}
          {converting ? " (≈ CAD)" : ""}
        </CardTitle>
        <CardDescription>{caption}</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading || waitingOnRates ? (
          <Skeleton className="h-[240px] w-full" />
        ) : !hasAny ? (
          <div className="flex h-[160px] items-center justify-center rounded-lg border border-dashed border-muted text-sm text-muted-foreground">
            No activity in {year} yet.
          </div>
        ) : (
          <div
            role="img"
            aria-label={`Money in versus money out per month for ${year}`}
          >
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={chartData} barGap={2}>
                <CartesianGrid stroke="hsl(40, 15%, 90%)" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 12, fill: "hsl(30, 5%, 47%)" }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  tickFormatter={formatCompactAxis}
                  tick={{ fontSize: 11, fill: "hsl(30, 5%, 47%)" }}
                  tickLine={false}
                  axisLine={false}
                  width={52}
                />
                <Tooltip
                  content={<CustomTooltip approx={approx} />}
                  cursor={{ fill: "rgba(55, 53, 47, 0.04)" }}
                />
                <Legend wrapperStyle={{ fontSize: "12px", paddingTop: "8px" }} />
                <Bar
                  dataKey="Money in"
                  fill={CHART_GREEN}
                  radius={[4, 4, 0, 0]}
                  maxBarSize={28}
                  cursor="pointer"
                  onClick={handleClick}
                >
                  {chartData.map((d) => (
                    <Cell
                      key={`in-${d.month}`}
                      fill={CHART_GREEN}
                      opacity={dimmed(d.month) ? 0.35 : 1}
                    />
                  ))}
                </Bar>
                <Bar
                  dataKey="Money out"
                  fill={CHART_AMBER}
                  radius={[4, 4, 0, 0]}
                  maxBarSize={28}
                  cursor="pointer"
                  onClick={handleClick}
                >
                  {chartData.map((d) => (
                    <Cell
                      key={`out-${d.month}`}
                      fill={CHART_AMBER}
                      opacity={dimmed(d.month) ? 0.35 : 1}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
