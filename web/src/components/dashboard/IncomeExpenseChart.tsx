import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

interface CurrencyMoney {
  currency: string;
  income: number;
  expenses: number;
  net: number;
}

interface MonthStatus {
  month: number;
  transaction_count: number;
  resolved_count: number;
  unresolved_count: number;
  income: number;
  expenses: number;
  by_currency?: CurrencyMoney[];
  status: string;
}

interface MonthlyStatusResponse {
  year: number;
  months: MonthStatus[];
}

const MONTH_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function currencyPrefix(currency: string): string {
  if (!currency) return "$";
  if (currency === "CAD") return "C$";
  if (currency === "USD") return "US$";
  return `${currency} `;
}

interface CustomTooltipProps {
  active?: boolean;
  payload?: Array<{ value: number; name: string; color: string }>;
  label?: string;
  currency: string;
}

function CustomTooltip({ active, payload, label, currency }: CustomTooltipProps) {
  if (!active || !payload) return null;
  return (
    <div className="rounded-lg border bg-white px-3 py-2 shadow-warm-md text-sm">
      <p className="font-semibold text-foreground mb-1">{label}</p>
      {payload.map((entry, i) => (
        <p key={i} className="text-xs" style={{ color: entry.color }}>
          {entry.name}: {currencyPrefix(currency)}
          {entry.value.toLocaleString("en-CA", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}
        </p>
      ))}
    </div>
  );
}

/**
 * Revenue vs Expenses bars. One currency is charted at a time — plotting
 * CAD and USD amounts on the same numeric axis would compare them at par.
 * Multi-currency users get a currency toggle.
 */
export function IncomeExpenseChart() {
  const { data, isLoading } = useQuery<MonthlyStatusResponse>({
    queryKey: ["dashboard", "monthly-status"],
    queryFn: () => api.get("/api/dashboard/monthly-status"),
  });

  const currencies = useMemo(() => {
    const set = new Set<string>();
    for (const m of data?.months ?? []) {
      for (const c of m.by_currency ?? []) set.add(c.currency);
    }
    // CAD first, then USD, then alphabetical.
    return [...set].sort((a, b) => {
      const rank = (c: string) => (c === "CAD" ? 0 : c === "USD" ? 1 : 2);
      return rank(a) - rank(b) || a.localeCompare(b);
    });
  }, [data]);

  const [selected, setSelected] = useState<string | null>(null);
  const currency = selected && currencies.includes(selected)
    ? selected
    : currencies[0] ?? "CAD";

  if (isLoading || !data) {
    return (
      <Card className="shadow-warm rounded-xl">
        <CardHeader>
          <CardTitle className="text-lg font-bold">Revenue vs Expenses</CardTitle>
          <CardDescription>Monthly comparison</CardDescription>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-[250px] w-full" />
        </CardContent>
      </Card>
    );
  }

  // Filter to months with activity in the selected currency. Falls back to
  // the legacy cross-currency fields only when by_currency is absent
  // (stale backend), with a bare $ axis.
  const hasPerCurrency = currencies.length > 0;
  const chartData = data.months
    .filter((m) => m.transaction_count > 0)
    .map((m) => {
      if (!hasPerCurrency) {
        return {
          month: MONTH_SHORT[m.month - 1],
          Income: Math.round(m.income),
          Expenses: Math.round(Math.abs(m.expenses)),
        };
      }
      const c = (m.by_currency ?? []).find((x) => x.currency === currency);
      return {
        month: MONTH_SHORT[m.month - 1],
        Income: Math.round(c?.income ?? 0),
        Expenses: Math.round(Math.abs(c?.expenses ?? 0)),
      };
    })
    .filter((row) => row.Income !== 0 || row.Expenses !== 0);

  const axisPrefix = hasPerCurrency ? currencyPrefix(currency) : "$";
  const formatCompactAxis = (value: number): string => {
    if (value >= 1000) return `${axisPrefix}${(value / 1000).toFixed(0)}K`;
    return `${axisPrefix}${value.toFixed(0)}`;
  };

  if (chartData.length === 0) {
    return (
      <Card className="shadow-warm rounded-xl">
        <CardHeader>
          <CardTitle className="text-lg font-bold">Revenue vs Expenses</CardTitle>
          <CardDescription>Monthly comparison</CardDescription>
        </CardHeader>
        <CardContent className="flex items-center justify-center py-12">
          <p className="text-sm text-muted-foreground">
            No transaction data yet. Upload bank statements to see your income and expenses.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader className="flex flex-row items-start justify-between space-y-0">
        <div>
          <CardTitle className="text-lg font-bold">Revenue vs Expenses</CardTitle>
          <CardDescription>
            Monthly comparison for {data.year}
            {currencies.length > 1 ? ` · ${currency} only` : ""}
          </CardDescription>
        </div>
        {currencies.length > 1 && (
          <div
            className="flex rounded-lg border bg-muted/30 p-0.5"
            role="tablist"
            aria-label="Chart currency"
          >
            {currencies.map((c) => (
              <button
                key={c}
                role="tab"
                aria-selected={c === currency}
                onClick={() => setSelected(c)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  c === currency
                    ? "bg-white text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {c}
              </button>
            ))}
          </div>
        )}
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={250}>
          <BarChart data={chartData} barGap={2}>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="hsl(40, 15%, 90%)"
              vertical={false}
            />
            <XAxis
              dataKey="month"
              tick={{ fontSize: 12, fill: "hsl(30, 5%, 47%)" }}
              tickLine={false}
              axisLine={false}
            />
            <YAxis
              tickFormatter={formatCompactAxis}
              tick={{ fontSize: 11, fill: "hsl(30, 5%, 47%)" }}
              tickLine={false}
              axisLine={false}
              width={58}
            />
            <Tooltip
              content={<CustomTooltip currency={hasPerCurrency ? currency : ""} />}
              cursor={{ fill: "rgba(55, 53, 47, 0.04)" }}
            />
            <Legend
              wrapperStyle={{ fontSize: "12px", paddingTop: "8px" }}
            />
            <Bar
              dataKey="Income"
              fill="hsl(var(--success))"
              radius={[4, 4, 0, 0]}
              maxBarSize={32}
            />
            <Bar
              dataKey="Expenses"
              fill="hsl(var(--accent))"
              radius={[4, 4, 0, 0]}
              maxBarSize={32}
            />
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
