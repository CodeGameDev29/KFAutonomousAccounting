import { useNavigate } from "react-router-dom";
import { Logo } from "@/components/Logo";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import {
  ArrowLeft,
  CheckCircle,
  Link2,
  AlertCircle,
} from "lucide-react";

// Invented rows for an invented company — the same cast the synthetic test
// fixtures use (see tests/fixtures/README.md). No merchant, amount, date or
// receipt filename here comes from anyone's books.
const SAMPLE_TRANSACTIONS = [
  {
    date: "2026-03-02",
    description: "Orchid Telecom - Business Internet",
    amount: -67.2,
    currency: "CAD",
    status: "MATCHED",
    confidence: 97,
    receipt: "20260302-OrchidTelecom-Invoice.pdf",
    explanation: "Exact amount match ($67.20 CAD). Vendor 'Orchid Telecom' matched by name. Date within 1 day of bank posting.",
  },
  {
    date: "2026-03-05",
    description: "Bluepeak Software Inc",
    amount: -145.0,
    currency: "USD",
    status: "MATCHED",
    confidence: 94,
    receipt: "20260305-Bluepeak-Invoice.pdf",
    explanation: "Amount matched within FX tolerance ($145.00 USD). Vendor identified as 'Bluepeak Software Inc' via receipt extraction.",
  },
  {
    date: "2026-03-07",
    description: "Cedarview Supplies Ltd - Monthly Plan",
    amount: -90.0,
    currency: "CAD",
    status: "MATCHED",
    confidence: 99,
    receipt: "20260307-Cedarview-Invoice.pdf",
    explanation: "Exact amount and vendor match. Recurring monthly charge pattern detected.",
  },
  {
    date: "2026-03-10",
    description: "Anytown Utility Co - Electricity",
    amount: -65.0,
    currency: "CAD",
    status: "LINKED",
    confidence: 88,
    receipt: null,
    explanation: "Cross-statement link: payment matches the utility's credit card charge from the previous month's statement.",
  },
  {
    date: "2026-03-12",
    description: "Anytown Coffee House - Client Meeting",
    amount: -25.0,
    currency: "CAD",
    status: "MATCHED",
    confidence: 91,
    receipt: "20260312-AnytownCoffee-Receipt.pdf",
    explanation: "Amount matched ($25.00 CAD). Date within 0 days. Category: Meals and entertainment.",
  },
  {
    date: "2026-03-15",
    description: "Client Payment - Northwind Trading",
    amount: 4500.0,
    currency: "USD",
    status: "MATCHED",
    confidence: 96,
    receipt: "20260315-Northwind-Invoice.pdf",
    explanation: "Income matched to outstanding invoice #INV-2026-042. Amount: $4,500.00 USD.",
  },
  {
    date: "2026-03-18",
    description: "Example Retailer - Office Supplies",
    amount: -155.0,
    currency: "CAD",
    status: "UNMATCHED",
    confidence: null,
    receipt: null,
    explanation: null,
  },
  {
    date: "2026-03-20",
    description: "Maplewing Air - Domestic Return Flight",
    amount: -389.0,
    currency: "CAD",
    status: "MATCHED",
    confidence: 93,
    receipt: "20260320-MaplewingAir-Receipt.pdf",
    explanation: "Amount matched ($389.00 CAD). Vendor identified as the airline. Category: Travel.",
  },
  {
    date: "2026-03-22",
    description: "Pinegrove Mobile - Mobile Plan",
    amount: -75.0,
    currency: "CAD",
    status: "MATCHED",
    confidence: 98,
    receipt: "20260322-PinegroveMobile-Bill.pdf",
    explanation: "Exact recurring charge match. Vendor and amount consistent with prior months.",
  },
  {
    date: "2026-03-25",
    description: "Meridian Fuel",
    amount: -60.0,
    currency: "CAD",
    status: "MATCHED",
    confidence: 85,
    receipt: "20260325-MeridianFuel-Receipt.pdf",
    explanation: "Amount matched within $0.05 tolerance. Date within 1 day of posting.",
  },
  {
    date: "2026-03-28",
    description: "Monthly Account Fee",
    amount: -5.0,
    currency: "CAD",
    status: "LINKED",
    confidence: null,
    receipt: null,
    explanation: "Auto-excluded: bank fee (non-reconcilable item).",
  },
  {
    date: "2026-03-30",
    description: "Client Payment - Harbourline Logistics",
    amount: 3200.0,
    currency: "CAD",
    status: "MATCHED",
    confidence: 95,
    receipt: "20260330-Harbourline-Invoice.pdf",
    explanation: "Income matched to invoice #INV-2026-048. Amount: $3,200.00 CAD.",
  },
];

const STATUS_STYLES: Record<string, { bg: string; text: string; icon: React.ComponentType<{ className?: string }> }> = {
  MATCHED: { bg: "bg-success/10", text: "text-success", icon: CheckCircle },
  LINKED: { bg: "bg-primary/10", text: "text-primary", icon: Link2 },
  UNMATCHED: { bg: "bg-muted", text: "text-muted-foreground", icon: AlertCircle },
};

export function SampleReport() {
  const navigate = useNavigate();

  const totalTxn = SAMPLE_TRANSACTIONS.length;
  const matchedTxn = SAMPLE_TRANSACTIONS.filter(
    (t) => t.status === "MATCHED" || t.status === "LINKED"
  ).length;
  const resolutionRate = Math.round((matchedTxn / totalTxn) * 100);

  // Net per currency — the sample data has CAD and USD rows, and amounts in
  // different currencies are never numerically summed (same rule the real
  // product follows everywhere).
  const netByCurrency = SAMPLE_TRANSACTIONS.reduce<Record<string, number>>(
    (acc, t) => {
      acc[t.currency] = (acc[t.currency] ?? 0) + t.amount;
      return acc;
    },
    {}
  );
  const netEntries = Object.entries(netByCurrency).sort(([a], [b]) =>
    a === "CAD" ? -1 : b === "CAD" ? 1 : a.localeCompare(b)
  );

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <Logo className="h-8 w-8" />
            <div>
              <h1 className="text-lg font-semibold text-foreground">Sample Report</h1>
              <p className="text-xs text-muted-foreground">
                Example Corp Inc. — March 2026
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Badge className="bg-amber-100 text-amber-700 border-amber-200">
              SAMPLE
            </Badge>
          </div>
        </div>

        {/* Summary Cards */}
        <div className="grid gap-4 md:grid-cols-3 mb-8">
          <Card>
            <CardContent className="pt-6">
              <p className="text-xs text-muted-foreground">Total Transactions</p>
              <p className="text-2xl font-bold text-foreground">{totalTxn}</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-6">
              <p className="text-xs text-muted-foreground">Match Rate</p>
              <p className="text-2xl font-bold text-success">{resolutionRate}%</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-6">
              <p className="text-xs text-muted-foreground">Net Income</p>
              {netEntries.map(([cur, net]) => (
                <p
                  key={cur}
                  className={`text-2xl font-bold ${net >= 0 ? "text-success" : "text-destructive"}`}
                >
                  {net < 0 ? "−" : ""}
                  {cur === "USD" ? "US$" : "C$"}
                  {Math.abs(net).toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </p>
              ))}
            </CardContent>
          </Card>
        </div>

        {/* Transactions Table */}
        <Card className="mb-8">
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/30">
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground">Date</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground">Description</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-muted-foreground">Amount</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground">Status</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground">Matched Receipt</th>
                  </tr>
                </thead>
                <tbody>
                  {SAMPLE_TRANSACTIONS.map((txn, i) => {
                    const style = STATUS_STYLES[txn.status] ?? STATUS_STYLES.UNMATCHED;
                    const Icon = style.icon;
                    return (
                      <tr key={i} className="border-b last:border-0 hover:bg-muted/20 transition-colors">
                        <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                          {new Date(`${txn.date}T00:00:00`).toLocaleDateString("en-CA", {
                            month: "short",
                            day: "numeric",
                          })}
                        </td>
                        <td className="px-4 py-3 text-sm font-medium text-foreground">
                          {txn.description}
                        </td>
                        <td className={`px-4 py-3 text-sm font-mono text-right whitespace-nowrap ${txn.amount >= 0 ? "text-success" : "text-foreground"}`}>
                          {txn.amount >= 0 ? "+" : "−"}
                          ${Math.abs(txn.amount).toLocaleString("en-CA", {
                            minimumFractionDigits: 2,
                            maximumFractionDigits: 2,
                          })}{" "}
                          <span className="text-xs text-muted-foreground">{txn.currency}</span>
                        </td>
                        <td className="px-4 py-3 text-center">
                          <Badge className={`${style.bg} ${style.text} border-0 text-[10px] gap-1`}>
                            <Icon className="h-3 w-3" />
                            {txn.status}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-xs text-muted-foreground">
                          {txn.receipt ?? <span className="italic">—</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>

        <Separator className="mb-8" />

        {/* What this page is */}
        <div className="text-center space-y-4">
          <p className="text-sm text-muted-foreground">
            This is what a monthly report looks like. Every row on it is
            invented — no merchant, amount or receipt here belongs to anyone.
          </p>
          <div className="flex items-center justify-center gap-3">
            <Button
              variant="outline"
              size="sm"
              onClick={() => navigate("/")}
              className="gap-1.5"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Back to home
            </Button>
          </div>
        </div>

        {/* Footer */}
        <p className="text-center text-[10px] text-muted-foreground/50 mt-12">
          Sample layout of the monthly report. Autonomous Accounting.
        </p>
      </div>
    </div>
  );
}
