/** Sales-tax (GST/HST/PST) summary for a fiscal year, aggregated from extracted receipts. */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Download, ReceiptText } from "lucide-react";
import { cn } from "@/lib/utils";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";

interface TaxAmounts {
  gst: number;
  hst: number;
  pst: number;
  other: number;
  total_tax: number;
  taxed_receipt_count: number;
  receipt_count: number;
}

interface TaxMonthRow extends TaxAmounts {
  month: number;
}

interface TaxQuarterRow extends TaxAmounts {
  quarter: number;
}

interface CurrencyTaxSummary {
  currency: string;
  months: TaxMonthRow[];
  quarters: TaxQuarterRow[];
  total: TaxAmounts & { year: number };
}

interface UndatedTaxRow {
  currency: string;
  gst: number;
  hst: number;
  pst: number;
  other: number;
  total_tax: number;
  receipt_count: number;
}

interface TaxSummaryResponse {
  year: number;
  currencies: CurrencyTaxSummary[];
  undated: UndatedTaxRow[];
}

const MONTH_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatMoney(amount: number, currency: string): string {
  try {
    return amount.toLocaleString("en-CA", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
    });
  } catch {
    return `$${amount.toFixed(2)}`;
  }
}

function buildCsv(data: TaxSummaryResponse): string {
  const lines = [
    "Currency,Period,GST,HST,PST,Other,Total Tax,Receipts With Tax",
  ];
  for (const cur of data.currencies) {
    for (const m of cur.months) {
      if (m.receipt_count === 0 && m.total_tax === 0) continue;
      lines.push(
        `${cur.currency},${data.year}-${String(m.month).padStart(2, "0")},` +
          `${m.gst.toFixed(2)},${m.hst.toFixed(2)},${m.pst.toFixed(2)},` +
          `${m.other.toFixed(2)},${m.total_tax.toFixed(2)},${m.taxed_receipt_count}`
      );
    }
    for (const q of cur.quarters) {
      if (q.receipt_count === 0 && q.total_tax === 0) continue;
      lines.push(
        `${cur.currency},${data.year} Q${q.quarter},` +
          `${q.gst.toFixed(2)},${q.hst.toFixed(2)},${q.pst.toFixed(2)},` +
          `${q.other.toFixed(2)},${q.total_tax.toFixed(2)},${q.taxed_receipt_count}`
      );
    }
    const t = cur.total;
    lines.push(
      `${cur.currency},${data.year} Total,` +
        `${t.gst.toFixed(2)},${t.hst.toFixed(2)},${t.pst.toFixed(2)},` +
        `${t.other.toFixed(2)},${t.total_tax.toFixed(2)},${t.taxed_receipt_count}`
    );
  }
  for (const u of data.undated) {
    lines.push(
      `${u.currency},Undated,` +
        `${u.gst.toFixed(2)},${u.hst.toFixed(2)},${u.pst.toFixed(2)},` +
        `${u.other.toFixed(2)},${u.total_tax.toFixed(2)},${u.receipt_count}`
    );
  }
  return lines.join("\n");
}

function downloadCsv(data: TaxSummaryResponse) {
  const blob = new Blob([buildCsv(data)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `TaxSummary_${data.year}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function CurrencyTable({
  summary,
  year,
}: {
  summary: CurrencyTaxSummary;
  year: number;
}) {
  const visibleMonths = summary.months.filter(
    (m) => m.receipt_count > 0 || m.total_tax !== 0
  );
  if (visibleMonths.length === 0) return null;
  // A currency with receipts but zero extracted tax all year renders as a
  // table of $0.00 rows — pure noise. Skip it.
  if (summary.total.total_tax === 0) return null;

  // PST and Other are uncommon outside specific provinces/vendors — hide their
  // columns when the whole year has nothing, to keep the table scannable.
  const showPst = summary.total.pst !== 0;
  const showOther = summary.total.other !== 0;

  const fmt = (n: number) => formatMoney(n, summary.currency);

  const rows: Array<
    | { kind: "month"; row: TaxMonthRow }
    | { kind: "quarter"; row: TaxQuarterRow }
  > = [];
  for (let q = 0; q < 4; q++) {
    const monthsInQuarter = visibleMonths.filter(
      (m) => Math.floor((m.month - 1) / 3) === q
    );
    for (const m of monthsInQuarter) rows.push({ kind: "month", row: m });
    const quarter = summary.quarters[q];
    if (monthsInQuarter.length > 1 && quarter) {
      rows.push({ kind: "quarter", row: quarter });
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs uppercase tracking-wider text-muted-foreground">
            <th className="py-2 pr-4 font-semibold">
              Period{summary.currency !== "CAD" ? ` (${summary.currency})` : ""}
            </th>
            <th className="py-2 px-3 text-right font-semibold">GST</th>
            <th className="py-2 px-3 text-right font-semibold">HST</th>
            {showPst && (
              <th className="py-2 px-3 text-right font-semibold">PST</th>
            )}
            {showOther && (
              <th className="py-2 px-3 text-right font-semibold">Other</th>
            )}
            <th className="py-2 px-3 text-right font-semibold">Total</th>
            <th className="py-2 pl-3 text-right font-semibold">Receipts</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((entry) =>
            entry.kind === "month" ? (
              <tr key={`m-${entry.row.month}`} className="border-b border-border/40">
                <td className="py-2 pr-4">
                  {MONTH_SHORT[entry.row.month - 1]} {year}
                </td>
                <td className="py-2 px-3 text-right font-mono">{fmt(entry.row.gst)}</td>
                <td className="py-2 px-3 text-right font-mono">{fmt(entry.row.hst)}</td>
                {showPst && (
                  <td className="py-2 px-3 text-right font-mono">{fmt(entry.row.pst)}</td>
                )}
                {showOther && (
                  <td className="py-2 px-3 text-right font-mono">{fmt(entry.row.other)}</td>
                )}
                <td className="py-2 px-3 text-right font-mono font-medium">
                  {fmt(entry.row.total_tax)}
                </td>
                <td className="py-2 pl-3 text-right font-mono text-muted-foreground">
                  {entry.row.taxed_receipt_count}
                </td>
              </tr>
            ) : (
              <tr
                key={`q-${entry.row.quarter}`}
                className="border-b border-border/40 bg-muted/30 text-muted-foreground"
              >
                <td className="py-1.5 pr-4 text-xs font-semibold">
                  Q{entry.row.quarter} subtotal
                </td>
                <td className="py-1.5 px-3 text-right font-mono text-xs">{fmt(entry.row.gst)}</td>
                <td className="py-1.5 px-3 text-right font-mono text-xs">{fmt(entry.row.hst)}</td>
                {showPst && (
                  <td className="py-1.5 px-3 text-right font-mono text-xs">{fmt(entry.row.pst)}</td>
                )}
                {showOther && (
                  <td className="py-1.5 px-3 text-right font-mono text-xs">{fmt(entry.row.other)}</td>
                )}
                <td className="py-1.5 px-3 text-right font-mono text-xs font-semibold">
                  {fmt(entry.row.total_tax)}
                </td>
                <td className="py-1.5 pl-3 text-right font-mono text-xs">
                  {entry.row.taxed_receipt_count}
                </td>
              </tr>
            )
          )}
          <tr className="font-semibold">
            <td className="py-2.5 pr-4">{year} total</td>
            <td className="py-2.5 px-3 text-right font-mono">{fmt(summary.total.gst)}</td>
            <td className="py-2.5 px-3 text-right font-mono">{fmt(summary.total.hst)}</td>
            {showPst && (
              <td className="py-2.5 px-3 text-right font-mono">{fmt(summary.total.pst)}</td>
            )}
            {showOther && (
              <td className="py-2.5 px-3 text-right font-mono">{fmt(summary.total.other)}</td>
            )}
            <td className="py-2.5 px-3 text-right font-mono text-primary">
              {fmt(summary.total.total_tax)}
            </td>
            <td className="py-2.5 pl-3 text-right font-mono">
              {summary.total.taxed_receipt_count}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

export function TaxSummaryCard({ year }: { year: number }) {
  const { data, isLoading, isError, error, refetch } = useQuery<TaxSummaryResponse>({
    queryKey: ["reports", year, "tax-summary"],
    queryFn: () => api.get(`/api/reports/${year}/tax-summary`),
  });

  // "Has data" = at least one currency actually has extracted tax (a year of
  // receipts with $0.00 tax everywhere shows the empty state instead).
  const hasData =
    !!data &&
    (data.currencies.some((c) => c.total.total_tax !== 0) ||
      data.undated.length > 0);

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <div className="h-8 w-8 rounded-lg bg-primary/10 flex items-center justify-center">
              <ReceiptText className="h-4 w-4 text-primary" />
            </div>
            <div>
              <CardTitle className="text-lg font-bold">
                Sales Tax Summary
              </CardTitle>
              <p className="text-xs text-muted-foreground">
                GST / HST / PST extracted from your receipts, by month and quarter
              </p>
            </div>
          </div>
          {hasData && (
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5 border-primary/20 text-primary hover:bg-primary/5"
              onClick={() => data && downloadCsv(data)}
            >
              <Download className="h-4 w-4" />
              CSV
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className={cn("space-y-6", !hasData && "pb-6")}>
        {isError ? (
          <ErrorDisplay error={error} context="tax summary" onRetry={refetch} />
        ) : isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-5 w-full" />
            <Skeleton className="h-5 w-full" />
            <Skeleton className="h-5 w-2/3" />
          </div>
        ) : !hasData ? (
          <p className="text-sm text-muted-foreground">
            No tax amounts extracted for {year} yet. GST, HST and PST captured
            from your uploaded receipts will be summarized here.
          </p>
        ) : (
          <>
            {data.currencies.map((cur) => (
              <CurrencyTable key={cur.currency} summary={cur} year={year} />
            ))}
            {data.undated.map((u) => (
              <p key={u.currency} className="text-xs text-amber-700">
                {u.receipt_count} receipt{u.receipt_count === 1 ? "" : "s"} with{" "}
                {formatMoney(u.total_tax, u.currency)} {u.currency} in tax{" "}
                {u.receipt_count === 1 ? "has" : "have"} no readable date and{" "}
                {u.receipt_count === 1 ? "is" : "are"} not included above — fix the
                date on the Receipts page to place {u.receipt_count === 1 ? "it" : "them"}.
              </p>
            ))}
            <p className="text-xs text-muted-foreground">
              Totals are sums of tax amounts extracted from your uploaded
              receipts. Verify against source documents before filing a return.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
