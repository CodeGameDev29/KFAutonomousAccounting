import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  flashRowWhenMounted,
  HIGHLIGHT_ROW_CSS,
  useResizableColumns,
  type ResizableColumnDef,
} from "@/components/reports/resizable-columns";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Clock,
  Link2,
  Paperclip,
  Printer,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import { Logo } from "@/components/Logo";
import { ConfidenceIndicator } from "@/components/ui/confidence-indicator";
import { API_URL } from "@/lib/constants";
import type {
  AccountGroup,
  MonthDetailResponse,
  SharedReceiptResponse,
  Transaction,
} from "@/components/reports/types";
import { formatCurrency, formatDate, formatDatetime } from "@/components/reports/format";

// --- Unauthenticated fetch for shared reports ---

async function fetchSharedReport(
  token: string
): Promise<MonthDetailResponse & { expires_at?: string; company_name?: string; generated_at?: string }> {
  const res = await fetch(`${API_URL}/api/shared/${token}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Error ${res.status}`);
  }
  return res.json();
}

async function fetchSharedReceiptUrl(
  token: string,
  docId: number
): Promise<SharedReceiptResponse> {
  const res = await fetch(`${API_URL}/api/shared/${token}/receipt/${docId}`);
  if (!res.ok) {
    throw new Error(`${res.status}: Failed to load receipt`);
  }
  return res.json();
}

// --- Format helpers ---

function formatAmount(amount: string, currency: string): string {
  // Magnitude only — the caller supplies the +/- sign from the transaction
  // TYPE. Stored signs vary by ingest path, so re-attaching the raw sign here
  // would double it ("--$123.45" / "+-$123.45").
  const num = parseFloat(amount);
  if (currency === "CAD" || currency === "USD") {
    return new Intl.NumberFormat("en-CA", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(Math.abs(num));
  }
  // Never render a non-CAD/USD currency (e.g. EUR) with a dollar sign.
  return `${Math.abs(num).toLocaleString("en-CA", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency}`;
}

function fallbackAccountCurrency(account: AccountGroup): string {
  if (account.transactions.length > 0) {
    return account.transactions[0].currency;
  }
  const name = account.account.toUpperCase();
  if (name.includes("USD")) return "USD";
  return "CAD";
}

// --- Status badge (mirrors AccountDetail so shared view matches owner view) ---

function StatusBadge({ status }: { status: Transaction["status"] }) {
  switch (status) {
    case "MATCHED":
      return (
        <Badge className="bg-primary/10 text-primary hover:bg-primary/10 border-primary/20">
          Matched
        </Badge>
      );
    case "LINKED":
      return (
        <Badge className="bg-sky-50 text-sky-700 hover:bg-sky-50 border-sky-200">
          Linked
        </Badge>
      );
    case "UNMATCHED":
      return <Badge variant="secondary">Unmatched</Badge>;
    case "PENDING_REVIEW":
      return (
        <Badge className="bg-amber-50 text-amber-700 hover:bg-amber-50 border-amber-200">
          Review
        </Badge>
      );
    // Self-evident row — no receipt needed, but the amount still counts.
    // The accountant reading this report must not think the money was dropped.
    case "IGNORED":
      return (
        <Badge variant="outline" className="border-emerald-200 text-emerald-700 dark:border-emerald-900 dark:text-emerald-300">
          No receipt
        </Badge>
      );
    case "EXCLUDED":
      return (
        <Badge variant="outline" className="text-muted-foreground line-through">
          Excluded
        </Badge>
      );
    default:
      return <Badge variant="secondary">{status}</Badge>;
  }
}

// --- Resizable column config ----------------------------------------------

// Same column order as the in-app tables (Date, Description, Category,
// Amount, Status, ...). Currency is embedded in the amount (C$ / US$ /
// "1,234.56 EUR") rather than a separate column.
const COLUMNS: ResizableColumnDef[] = [
  { key: "date", label: "Date", initial: 110, min: 90 },
  { key: "description", label: "Description", initial: 360, min: 160 },
  { key: "category", label: "Category", initial: 200, min: 90 },
  { key: "amount", label: "Amount", initial: 140, min: 100, align: "right" },
  { key: "status", label: "Status", initial: 120, min: 80 },
  { key: "confidence", label: "Confidence", initial: 100, min: 70, align: "center" },
  { key: "vendor", label: "Vendor", initial: 180, min: 90 },
  { key: "proof", label: "Proof of Transaction", initial: 130, min: 90, align: "center" },
];

// --- Transaction row for shared view ---

interface SharedTransactionRowProps {
  tx: Transaction;
  token: string;
  onReceiptError: () => void;
  onJumpToLinked: (targetId: number) => void;
}

function SharedTransactionRow({ tx, token, onReceiptError, onJumpToLinked }: SharedTransactionRowProps) {
  const [receiptError, setReceiptError] = useState<string | null>(null);
  const isCredit = tx.type === "CREDIT";

  async function handleViewReceipt() {
    if (!tx.receipt_doc_id) return;
    setReceiptError(null);
    try {
      const data = await fetchSharedReceiptUrl(token, tx.receipt_doc_id);
      window.open(data.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      const message = err instanceof Error ? err.message : "";
      if (message.includes("410")) {
        setReceiptError("This share link has expired. Ask the sender for an updated link.");
      } else {
        setReceiptError("Could not load this receipt. Please try again.");
      }
      onReceiptError();
    }
  }

  // Every counterpart, not just the first: a bulk charge refunded across
  // three credits has three, and showing one understates the audit trail in
  // the artifact an accountant actually reads.
  const linkedCounterparts = tx.linked_to_all ?? (tx.linked_to ? [tx.linked_to] : []);

  function handleLinkedClick(e: React.MouseEvent<HTMLButtonElement>, linkedId: number) {
    e.preventDefault();
    onJumpToLinked(linkedId);
  }

  return (
    <tr
      id={`txn-${tx.id}`}
      className="shared-tx-row border-b border-border/50 transition-colors hover:bg-muted/30"
    >
      <td className="whitespace-nowrap px-4 py-3 text-sm align-top">
        {formatDate(tx.date)}
      </td>
      <td className="px-4 py-3 text-sm align-top">
        <div className="whitespace-normal break-words">{tx.description}</div>
        {linkedCounterparts.map((linked, i) => (
          <button
            key={linked.id}
            type="button"
            onClick={(e) => handleLinkedClick(e, linked.id)}
            aria-label={`Jump to linked transaction ${linked.id}`}
            className="mt-1 flex max-w-full items-start gap-1 rounded px-1 -mx-1 text-left text-xs text-sky-700 hover:bg-sky-50 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 cursor-pointer"
          >
            <Link2 className="h-3 w-3 shrink-0 mt-0.5" />
            <span className="whitespace-normal break-words">
              {linkedCounterparts.length > 1
                ? `Linked (${i + 1} of ${linkedCounterparts.length}) to `
                : "Linked to "}
              <span className="font-medium">{linked.account}</span>
              {" · "}
              {formatDate(linked.date)}
              {" · "}
              {linked.type === "CREDIT" ? "+" : "-"}
              {formatAmount(linked.amount, linked.currency)}
              {" "}
              <span className="text-muted-foreground">({linked.link_type})</span>
            </span>
          </button>
        ))}
        {(tx.status === "MATCHED" || tx.status === "LINKED") && tx.match_explanation && (
          <div className="mt-1.5 text-xs text-muted-foreground/80 leading-relaxed">
            <span className="font-medium text-muted-foreground">Match reason:</span>{" "}
            {tx.match_explanation}
          </div>
        )}
        {tx.status === "PENDING_REVIEW" && (
          <div className="mt-1.5 text-xs leading-relaxed">
            <span className="font-medium text-amber-700">Needs review — here's what the engine found:</span>{" "}
            <span className="text-muted-foreground/80">
              {tx.match_explanation ?? "A potential match was found but confidence is below the auto-approve threshold."}
            </span>
          </div>
        )}
        {tx.status === "UNMATCHED" && (
          <div className="mt-1.5 text-xs leading-relaxed">
            <span className="font-medium text-muted-foreground/60">No matching transaction found.</span>
            {tx.match_explanation && (
              <span className="text-muted-foreground/50"> {tx.match_explanation}</span>
            )}
          </div>
        )}
      </td>
      <td className="px-4 py-3 text-sm text-muted-foreground align-top whitespace-normal break-words">
        {tx.category ?? "--"}
      </td>
      <td
        className={`whitespace-nowrap px-4 py-3 text-right text-sm font-mono font-medium align-top ${
          isCredit ? "text-primary" : "text-foreground"
        }`}
      >
        {isCredit ? "+" : "-"}
        {formatAmount(tx.amount, tx.currency)}
      </td>
      <td className="px-4 py-3 align-top">
        <StatusBadge status={tx.status} />
      </td>
      <td className="px-4 py-3 text-center align-top">
        {tx.match_score != null && ["MATCHED", "LINKED", "PENDING_REVIEW"].includes(tx.status) ? (
          <ConfidenceIndicator confidence={tx.match_score <= 1 ? tx.match_score * 100 : tx.match_score} />
        ) : (
          <span className="text-xs text-muted-foreground">--</span>
        )}
      </td>
      <td className="px-4 py-3 text-sm text-muted-foreground align-top whitespace-normal break-words">
        {tx.vendor ?? "--"}
      </td>
      <td className="px-4 py-3 text-center align-top">
        {tx.receipt_doc_id ? (
          <div>
            <Button
              variant="ghost"
              size="sm"
              className="h-auto px-2 py-1 text-xs text-primary hover:text-primary hover:bg-primary/5"
              onClick={handleViewReceipt}
            >
              <Paperclip className="mr-1 h-3 w-3" />
              View
            </Button>
            {receiptError && (
              <span className="text-xs text-amber-700 block mt-1">
                {receiptError}
              </span>
            )}
          </div>
        ) : tx.has_receipt ? (
          <span
            className="inline-flex items-center gap-1 text-xs text-muted-foreground italic"
            title="Matched, but the receipt file is no longer available."
          >
            <Paperclip className="h-3 w-3" />
            {tx.vendor ? `${tx.vendor.slice(0, 24)} (file missing)` : "(file missing)"}
          </span>
        ) : tx.status === "MATCHED" ? (
          <span
            className="inline-flex items-center gap-1 text-xs text-muted-foreground italic"
            title="Marked matched but no receipt linked."
          >
            {tx.vendor ? `${tx.vendor.slice(0, 24)} (no link)` : "Matched (no link)"}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">--</span>
        )}
      </td>
    </tr>
  );
}

// --- Shared transaction table ---

interface SharedTransactionTableProps {
  account: AccountGroup;
  token: string;
  widths: Record<string, number>;
  beginResize: (key: string, e: React.MouseEvent<HTMLDivElement>) => void;
  onReceiptError: () => void;
  onJumpToLinked: (targetId: number) => void;
}

function SharedTransactionTable({
  account,
  token,
  widths,
  beginResize,
  onReceiptError,
  onJumpToLinked,
}: SharedTransactionTableProps) {
  if (account.transactions.length === 0) {
    return (
      <div className="flex h-[200px] items-center justify-center text-sm text-muted-foreground">
        No transactions for this account.
      </div>
    );
  }

  const totalWidth = COLUMNS.reduce((s, c) => s + widths[c.key], 0);

  return (
    <div className="overflow-x-auto">
      <table className="text-left" style={{ tableLayout: "fixed", width: totalWidth }}>
        <colgroup>
          {COLUMNS.map((c) => (
            <col key={c.key} style={{ width: widths[c.key] }} />
          ))}
        </colgroup>
        <thead>
          <tr className="border-b bg-muted/30">
            {COLUMNS.map((c) => (
              <th
                key={c.key}
                className={`relative select-none px-4 py-3 text-xs font-medium uppercase tracking-wider text-muted-foreground ${
                  c.align === "right" ? "text-right" :
                  c.align === "center" ? "text-center" : "text-left"
                }`}
              >
                <span className="block truncate">{c.label}</span>
                {/* Column resize handle — drag to resize this column */}
                <div
                  role="separator"
                  aria-label={`Resize ${c.label} column`}
                  onMouseDown={(e) => beginResize(c.key, e)}
                  className="absolute top-0 right-0 h-full w-2 cursor-col-resize select-none group"
                >
                  <span className="absolute right-[3px] top-1/4 h-1/2 w-px bg-border/60 group-hover:bg-sky-500 group-active:bg-sky-600" />
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {account.transactions.map((tx) => (
            <SharedTransactionRow
              key={tx.id}
              tx={tx}
              token={token}
              onReceiptError={onReceiptError}
              onJumpToLinked={onJumpToLinked}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- Account summary ---

function SharedAccountSummary({ account }: { account: AccountGroup }) {
  // One Income/Expenses/Net group per currency — an account (e.g. Wise)
  // can hold several currencies; those are never numerically summed.
  const totals =
    account.currency_totals && account.currency_totals.length > 0
      ? account.currency_totals
      : [{
          currency: fallbackAccountCurrency(account),
          income: account.income,
          expenses: account.expenses,
          net: account.income - account.expenses,
        }];
  const multi = totals.length > 1;
  return (
    <div className="flex flex-col gap-y-1.5">
      {totals.map((t) => (
        <div
          key={t.currency}
          className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm"
        >
          {multi && (
            <span className="w-10 font-mono text-xs font-semibold text-muted-foreground">
              {t.currency}
            </span>
          )}
          <span>
            <span className="text-muted-foreground">Income:</span>{" "}
            <span className="font-mono font-medium text-primary">
              {formatCurrency(t.income, t.currency)}
            </span>
          </span>
          <span>
            <span className="text-muted-foreground">Expenses:</span>{" "}
            <span className="font-mono font-medium text-rose-600">
              {formatCurrency(t.expenses, t.currency)}
            </span>
          </span>
          <Separator orientation="vertical" className="h-4" />
          <span>
            <span className="text-muted-foreground">Net:</span>{" "}
            <span
              className={`font-mono font-semibold ${t.net >= 0 ? "text-primary" : "text-rose-600"}`}
            >
              {formatCurrency(t.net, t.currency)}
            </span>
          </span>
        </div>
      ))}
    </div>
  );
}

// --- Loading skeleton ---

function SharedSkeleton() {
  return (
    <div className="min-h-screen bg-background">
      <header className="border-b">
        <div className="container flex h-16 items-center justify-between">
          <div className="flex items-center gap-2">
            <Logo size="sm" />
            <span className="text-lg font-semibold">
              Autonomous Accounting
            </span>
            <Badge className="bg-primary/10 text-primary border-primary/20">Shared Report</Badge>
          </div>
        </div>
      </header>
      <main className="container max-w-[1500px] py-8">
        <div className="space-y-6">
          <Skeleton className="h-10 w-64" />
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-10 w-80" />
          <Card className="shadow-warm rounded-xl">
            <CardContent className="space-y-3 p-6">
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-3/4" />
            </CardContent>
          </Card>
        </div>
      </main>
    </div>
  );
}

// --- Expired / error state ---

function SharedError({ message }: { message: string }) {
  return (
    <div className="min-h-screen bg-background">
      <header className="border-b">
        <div className="container flex h-16 items-center">
          <div className="flex items-center gap-2">
            <Logo size="sm" />
            <span className="text-lg font-semibold">
              Autonomous Accounting
            </span>
          </div>
        </div>
      </header>
      <main className="container max-w-[1500px] py-8">
        <Card className="shadow-warm rounded-xl">
          <CardContent className="flex h-[300px] flex-col items-center justify-center gap-3">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-rose-50">
              <ShieldAlert className="h-7 w-7 text-rose-400" />
            </div>
            <div className="text-center">
              <p className="font-medium">Report unavailable</p>
              <p className="text-sm text-muted-foreground">{message}</p>
            </div>
          </CardContent>
        </Card>
      </main>
      <footer className="border-t py-6 text-center text-xs text-muted-foreground">
        Generated by Autonomous Accounting &middot; Designed around CRA record-keeping guidance (IC05-1R1) &mdash; not a certification
      </footer>
    </div>
  );
}

// --- Main SharedReport component ---

export function SharedReport() {
  const { token } = useParams<{ token: string }>();
  const [receiptFailureCount, setReceiptFailureCount] = useState(0);
  const [activeTab, setActiveTab] = useState<string>("");
  const { widths, beginResize } = useResizableColumns(COLUMNS);

  const { data, isLoading, error } = useQuery({
    queryKey: ["shared-report", token],
    queryFn: () => fetchSharedReport(token!),
    enabled: !!token,
    retry: false,
  });

  function handleReceiptError() {
    setReceiptFailureCount((prev) => prev + 1);
  }

  if (!token) {
    return <SharedError message="Invalid share link." />;
  }

  if (isLoading) {
    return <SharedSkeleton />;
  }

  if (error || !data) {
    const msg =
      error instanceof Error
        ? error.message
        : "This share link may have expired or is invalid.";
    return <SharedError message={msg} />;
  }

  const label = data.label || `Report`;
  const accounts = data.accounts ?? [];
  const defaultTab = accounts.length > 0 ? accounts[0].account : "";
  const companyName = (data as { company_name?: string }).company_name;
  const expiresAt = (data as { expires_at?: string }).expires_at;

  // Use backend resolution_rate (excludes IGNORED transactions) with fallback
  const resolutionRate = typeof (data as { resolution_rate?: number }).resolution_rate === "number"
    ? Math.round((data as { resolution_rate?: number }).resolution_rate!)
    : (() => {
        const totalTxn = accounts.reduce((s, a) => s + a.transaction_count, 0);
        const matchedTxn = accounts.reduce(
          (s, a) => s + a.transactions.filter(
            (t: Transaction) => t.status === "MATCHED" || t.status === "LINKED"
          ).length, 0
        );
        return totalTxn > 0 ? Math.round((matchedTxn / totalTxn) * 100) : 0;
      })();

  // Map each transaction id to the account tab it belongs to, so clicking a
  // "Linked to…" line on a row whose counterpart lives in a different tab can
  // switch tabs before scrolling + flashing the target row.
  const txnTabMap = new Map<number, string>();
  for (const acct of accounts) {
    for (const tx of acct.transactions) {
      if (tx.id != null) txnTabMap.set(tx.id, acct.account);
    }
  }

  function handleJumpToLinked(targetId: number) {
    const targetTab = txnTabMap.get(targetId);
    if (!targetTab) return;
    setActiveTab(targetTab);
    // Tab content mounts asynchronously; retry until the row appears in DOM.
    flashRowWhenMounted(`txn-${targetId}`);
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Inline CSS: row highlight animation for clicked "Linked to..." jumps */}
      <style>{HIGHLIGHT_ROW_CSS}</style>

      {/* Print stylesheet */}
      <style>{`
        @media print {
          @page { margin: 15mm 12mm 20mm 12mm; }

          /* Hide interactive elements */
          [role="tablist"],
          button:not(.print-visible),
          [role="separator"],
          .no-print {
            display: none !important;
          }

          /* Show all tab panels */
          [data-state="inactive"][role="tabpanel"] {
            display: block !important;
          }

          /* Table layout */
          table {
            width: 100%;
            font-size: 10pt;
            border-collapse: collapse;
          }

          th, td {
            border: 0.5pt solid #d1d5db;
            padding: 4pt 6pt;
          }

          /* Remove background colors from table rows */
          tr {
            background: white !important;
            page-break-inside: avoid;
          }

          /* Page breaks between sections */
          [role="tabpanel"] {
            page-break-before: always;
          }

          [role="tabpanel"]:first-of-type {
            page-break-before: avoid;
          }

          /* Preserve badge colors */
          .badge, [data-status] {
            print-color-adjust: exact;
            -webkit-print-color-adjust: exact;
          }

          /* Show print header */
          .print-header {
            display: block !important;
          }

          /* Show receipt references */
          .print-receipt-ref {
            display: inline !important;
          }
        }
      `}</style>

      {/* Print-only header */}
      <div className="hidden print:block print:mb-4 print:border-b print:pb-2 print-header">
        <p className="font-semibold">{companyName ?? "Autonomous Accounting"}</p>
        <p className="text-sm text-muted-foreground">{label} — Generated by Autonomous Accounting</p>
      </div>

      {/* Header */}
      <header className="border-b">
        <div className="container flex h-16 items-center justify-between">
          <div className="flex items-center gap-2">
            <Logo size="sm" />
            <span className="text-lg font-semibold">
              Autonomous Accounting
            </span>
            <Badge className="bg-primary/10 text-primary border-primary/20">Shared Report</Badge>
            {/* One counted fact, not a verdict: the share of transactions in
                this report that have a matched document. It must never read as
                a compliance status, because nobody has assessed one. */}
            {resolutionRate >= 80 ? (
              <Badge className="bg-success/10 text-success border-success/20">
                <ShieldCheck className="mr-1 h-3 w-3" />
                {resolutionRate}% with documents
              </Badge>
            ) : (
              <Badge className="bg-amber-50 text-amber-700 border-amber-200">
                <ShieldAlert className="mr-1 h-3 w-3" />
                {resolutionRate}% with documents
              </Badge>
            )}
          </div>
          <Button
            variant="outline"
            size="sm"
            className="no-print gap-1.5 border-primary/20 text-primary hover:bg-primary/5"
            onClick={() => window.print()}
          >
            <Printer className="h-4 w-4" />
            Print
          </Button>
        </div>
      </header>

      {/* Content */}
      <main className="container max-w-[1500px] py-8">
        <div className="space-y-6">
          {/* Title section */}
          <div>
            {companyName && (
              <p className="text-sm font-medium text-muted-foreground">
                {companyName}
              </p>
            )}
            <h1 className="text-3xl font-bold tracking-tight">{label}</h1>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-muted-foreground">
              <span>
                <span className="font-mono">{data.total_transactions}</span> transaction
                {data.total_transactions !== 1 ? "s" : ""} across{" "}
                <span className="font-mono">{accounts.length}</span> account{accounts.length !== 1 ? "s" : ""}
              </span>
              <span className="font-mono text-sm">
                {resolutionRate}% matched
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Report generated {formatDatetime(data.generated_at ?? new Date().toISOString())} · audit-binder layout
            </p>
          </div>

          {/* Expiry notice */}
          {expiresAt && (
            <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 shadow-warm-sm">
              <Clock className="h-4 w-4 shrink-0 text-amber-500" />
              <span>
                This report expires on{" "}
                <span className="font-mono font-medium">
                  {formatDatetime(expiresAt)}
                </span>
              </span>
            </div>
          )}

          {/* Receipt failure banner */}
          {receiptFailureCount >= 3 && (
            <div className="bg-amber-50 border border-amber-200 text-amber-800 px-4 py-3 rounded-lg text-sm mb-4">
              Some receipt links may have expired. Ask the report sender to share an updated link.
            </div>
          )}

          {/* Account tabs */}
          {accounts.length === 0 ? (
            <Card className="shadow-warm rounded-xl">
              <CardContent className="flex h-[300px] items-center justify-center">
                <p className="text-sm text-muted-foreground">
                  No transaction data in this report.
                </p>
              </CardContent>
            </Card>
          ) : (
            <Tabs
              value={activeTab || defaultTab}
              onValueChange={setActiveTab}
              className="space-y-4"
            >
              <TabsList>
                {accounts.map((acct) => (
                  <TabsTrigger key={acct.account} value={acct.account}>
                    {acct.account}
                    <Badge variant="secondary" className="ml-2 text-[10px] font-mono">
                      {acct.transaction_count}
                    </Badge>
                  </TabsTrigger>
                ))}
              </TabsList>

              {accounts.map((acct) => (
                <TabsContent key={acct.account} value={acct.account}>
                  <Card className="shadow-warm rounded-xl overflow-hidden">
                    <CardContent className="p-0">
                      <SharedTransactionTable
                        account={acct}
                        token={token}
                        widths={widths}
                        beginResize={beginResize}
                        onReceiptError={handleReceiptError}
                        onJumpToLinked={handleJumpToLinked}
                      />
                    </CardContent>
                    <CardFooter className="border-t px-4 py-3 bg-muted/20">
                      <SharedAccountSummary account={acct} />
                    </CardFooter>
                  </Card>
                </TabsContent>
              ))}
            </Tabs>
          )}
        </div>
      </main>

      {/* Footer */}
      <footer className="border-t py-6 text-center text-xs text-muted-foreground space-y-2">
        <p>
          Generated by Autonomous Accounting &middot; Designed around CRA record-keeping guidance (IC05-1R1) &mdash; not a certification
        </p>
        <p>
          Shared from a self-hosted instance. This link is read-only and expires.
        </p>
      </footer>
    </div>
  );
}
