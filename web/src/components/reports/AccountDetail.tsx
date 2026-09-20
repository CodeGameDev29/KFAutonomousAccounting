/** Account detail -- transaction tables for a specific month with export and management actions. */

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { withParams } from "@/lib/url-state";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { useAuthStore } from "@/stores/auth-store";
import {
  flashRowWhenMounted,
  HIGHLIGHT_ROW_CSS,
  useResizableColumns,
  type ResizableColumnDef,
} from "@/components/reports/resizable-columns";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Check,
  FileText,
  Link2,
  Loader2,
  Paperclip,
  Printer,
  X,
  ExternalLink,
} from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { formatCurrency, formatDate } from "@/components/reports/format";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import { DocumentCoverageBadge } from "@/components/reports/DocumentCoverageBadge";
import { ExportDropdown } from "@/components/reports/ExportDropdown";
import type {
  MonthReportResponse,
  MonthAccount,
  MonthTransaction,
  ShareLinkResponse,
} from "@/components/reports/types";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface AccountDetailProps {
  year: number;
  month: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function monthName(year: number, month: number): string {
  return new Date(year, month - 1).toLocaleString("en-CA", { month: "long" });
}

function formatAmount(amount: string, currency: string): string {
  const num = Math.abs(parseFloat(amount));
  // CAD/USD get their conventional symbols; any other currency renders as
  // "1,234.56 EUR" — never coerced to a dollar sign (same rule as the
  // Transactions page table).
  if (currency === "CAD" || currency === "USD") {
    return new Intl.NumberFormat("en-CA", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(num);
  }
  return `${num.toLocaleString("en-CA", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency}`;
}

// ---------------------------------------------------------------------------
// Status badge for individual transactions
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: MonthTransaction["status"] }) {
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

// ---------------------------------------------------------------------------
// Resizable column definitions for the transaction table
// ---------------------------------------------------------------------------

// Same column order as the Transactions page table (Date, Description,
// Category, Amount, Status, ...). Currency is shown inside the amount
// (C$ / US$ / "1,234.56 EUR") instead of a separate column.
const ACCOUNT_DETAIL_COLUMNS: ResizableColumnDef[] = [
  { key: "date", label: "Date", initial: 110, min: 90 },
  { key: "description", label: "Description", initial: 360, min: 160 },
  { key: "category", label: "Category", initial: 200, min: 90 },
  { key: "amount", label: "Amount", initial: 140, min: 100, align: "right" },
  { key: "status", label: "Status", initial: 120, min: 80 },
  { key: "vendor", label: "Vendor", initial: 180, min: 90 },
  { key: "receipt", label: "Receipt", initial: 130, min: 90, align: "center" },
];

// ---------------------------------------------------------------------------
// Single transaction row
// ---------------------------------------------------------------------------

interface TransactionRowProps {
  txn: MonthTransaction;
  onJumpToLinked: (targetId: number) => void;
  onViewReceipt?: (docId: number, description: string) => void;
}

function TransactionRow({ txn, onJumpToLinked, onViewReceipt }: TransactionRowProps) {
  const isCredit = txn.type ? txn.type === "CREDIT" : parseFloat(txn.amount) >= 0;

  function handleViewReceipt() {
    if (!txn.receipt_doc_id) return;
    if (onViewReceipt) {
      onViewReceipt(txn.receipt_doc_id, txn.description);
    }
  }

  // Every counterpart, not just the first: a bulk charge refunded across
  // three credits has three, and showing one understates the audit trail in
  // the artifact an accountant actually reads.
  const linkedCounterparts = txn.linked_to_all ?? (txn.linked_to ? [txn.linked_to] : []);

  function handleLinkedClick(e: React.MouseEvent<HTMLButtonElement>, linkedId: number) {
    e.preventDefault();
    onJumpToLinked(linkedId);
  }

  return (
    <tr
      id={`txn-${txn.id}`}
      className="detail-tx-row border-b border-border/50 transition-colors hover:bg-muted/30"
    >
      <td className="whitespace-nowrap px-4 py-3 text-sm align-top">
        {formatDate(txn.date)}
      </td>
      <td className="px-4 py-3 text-sm align-top">
        <div className="whitespace-normal break-words">{txn.description}</div>
        {txn.match_explanation && (
          <p className="mt-1 text-xs text-muted-foreground bg-muted/50 rounded px-2 py-1">
            {txn.match_explanation}
          </p>
        )}
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
      </td>
      <td className="px-4 py-3 text-sm text-muted-foreground align-top whitespace-normal break-words">
        {txn.category ?? "--"}
      </td>
      <td
        className={`whitespace-nowrap px-4 py-3 text-right text-sm font-mono font-medium align-top ${
          isCredit ? "text-primary" : "text-foreground"
        }`}
      >
        {isCredit ? "+" : "-"}
        {formatAmount(txn.amount, txn.currency)}
      </td>
      <td className="px-4 py-3 align-top">
        <StatusBadge status={txn.status} />
      </td>
      <td className="px-4 py-3 text-sm text-muted-foreground align-top whitespace-normal break-words">
        {txn.vendor ?? "--"}
      </td>
      <td className="px-4 py-3 text-center align-top">
        {txn.has_receipt && txn.receipt_doc_id ? (
          <Button
            variant="ghost"
            size="sm"
            className="h-auto px-2 py-1 text-xs text-primary hover:text-primary hover:bg-primary/5"
            onClick={handleViewReceipt}
            aria-label={`View receipt for ${txn.description}`}
          >
            <Paperclip className="mr-1 h-3 w-3" />
            Receipt
          </Button>
        ) : txn.has_receipt ? (
          <span
            className="inline-flex items-center gap-1 text-xs text-muted-foreground italic"
            title="Matched, but the receipt file is no longer available."
          >
            <Paperclip className="h-3 w-3" />
            {txn.vendor ? `${txn.vendor.slice(0, 24)} (file missing)` : "(file missing)"}
          </span>
        ) : txn.status === "MATCHED" ? (
          <span
            className="inline-flex items-center gap-1 text-xs text-muted-foreground italic"
            title="This transaction is marked matched but no receipt is linked."
          >
            {txn.vendor ? `${txn.vendor.slice(0, 24)} (no link)` : "Matched (no link)"}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">--</span>
        )}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Transaction table for a single account
// ---------------------------------------------------------------------------

interface AccountTransactionTableProps {
  account: MonthAccount;
  widths: Record<string, number>;
  beginResize: (key: string, e: React.MouseEvent<HTMLDivElement>) => void;
  onJumpToLinked: (targetId: number) => void;
  onViewReceipt?: (docId: number, description: string) => void;
}

function AccountTransactionTable({
  account,
  widths,
  beginResize,
  onJumpToLinked,
  onViewReceipt,
}: AccountTransactionTableProps) {
  if (account.transactions.length === 0) {
    return (
      <div className="flex h-[200px] items-center justify-center text-sm text-muted-foreground">
        No transactions for this account.
      </div>
    );
  }

  const totalWidth = ACCOUNT_DETAIL_COLUMNS.reduce((s, c) => s + widths[c.key], 0);

  return (
    <div className="overflow-x-auto">
      <table className="text-left" style={{ tableLayout: "fixed", width: totalWidth }}>
        <colgroup>
          {ACCOUNT_DETAIL_COLUMNS.map((c) => (
            <col key={c.key} style={{ width: widths[c.key] }} />
          ))}
        </colgroup>
        <thead>
          <tr className="border-b bg-muted/30">
            {ACCOUNT_DETAIL_COLUMNS.map((c) => (
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
          {account.transactions.map((txn) => (
            <TransactionRow key={txn.id} txn={txn} onJumpToLinked={onJumpToLinked} onViewReceipt={onViewReceipt} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Account summary footer
// ---------------------------------------------------------------------------

function fallbackAccountCurrency(account: MonthAccount): string {
  // Legacy fallback only (backend without currency_totals): first
  // transaction's currency, then account-name heuristic.
  if (account.transactions.length > 0) {
    return account.transactions[0].currency;
  }
  const name = account.account.toUpperCase();
  if (name.includes("USD")) return "USD";
  return "CAD";
}

function AccountSummary({ account }: { account: MonthAccount }) {
  // One Income/Expenses/Net group per currency — an account (e.g. Wise)
  // can hold several currencies and those are never summed together.
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
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span>
          <span className="text-muted-foreground">Transactions:</span>{" "}
          <span className="font-mono font-medium">{account.transaction_count}</span>
        </span>
      </div>
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

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function AccountDetailSkeleton() {
  return (
    <div className="space-y-6 pt-4">
      <div className="flex items-center gap-3">
        <Skeleton className="h-9 w-9 rounded-md" />
        <div className="space-y-2">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-5 w-40" />
        </div>
      </div>
      <div className="flex gap-2">
        <Skeleton className="h-10 w-32" />
        <Skeleton className="h-10 w-40" />
        <Skeleton className="h-10 w-24" />
        <Skeleton className="h-10 w-32" />
      </div>
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
  );
}

// ---------------------------------------------------------------------------
// Share link result banner
// ---------------------------------------------------------------------------

function ShareBanner({
  shareUrl,
  onDismiss,
}: {
  shareUrl: string;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const fullUrl = `${window.location.origin}${shareUrl}`;

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(fullUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback: select text
    }
  }

  return (
    <div className="flex items-center justify-between rounded-xl border border-primary/20 bg-primary/5 px-4 py-3 shadow-warm-sm">
      <div className="flex items-center gap-2 text-sm">
        <Link2 className="h-4 w-4 text-primary" />
        <span className="font-medium">Share link created:</span>
        <code className="rounded-md bg-muted px-2 py-0.5 text-xs font-mono">
          {fullUrl}
        </code>
      </div>
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={handleCopy}
          className="border-primary/20 text-primary hover:bg-primary/5"
        >
          {copied ? (
            <>
              <Check className="h-3.5 w-3.5" />
              Copied
            </>
          ) : (
            "Copy"
          )}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function AccountDetail({ year, month }: AccountDetailProps) {
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  // URL-backed so back/forward restores the selected account tab.
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get("account") ?? "";
  const setActiveTab = (v: string) =>
    setSearchParams((prev) => withParams(prev, { account: v }));
  const { widths, beginResize } = useResizableColumns(ACCOUNT_DETAIL_COLUMNS);
  const [receiptSlideOver, setReceiptSlideOver] = useState<{
    docId: number;
    description: string;
    blobUrl: string | null;
    loading: boolean;
    error: boolean;
  } | null>(null);

  const handleViewReceipt = async (docId: number, description: string) => {
    setReceiptSlideOver({ docId, description, blobUrl: null, loading: true, error: false });
    try {
      const { getAccessToken } = useAuthStore.getState();
      const token = await getAccessToken();
      const res = await fetch(`${API_URL}/api/receipts/${docId}/preview`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      // Retry once on 403/410 (expired signed URL)
      if (res.status === 403 || res.status === 410) {
        const retryRes = await fetch(`${API_URL}/api/receipts/${docId}/preview`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (retryRes.ok) {
          const blob = await retryRes.blob();
          setReceiptSlideOver((prev) => prev ? { ...prev, blobUrl: URL.createObjectURL(blob), loading: false } : null);
          return;
        }
      }
      if (!res.ok) {
        setReceiptSlideOver((prev) => prev ? { ...prev, loading: false, error: true } : null);
        return;
      }
      const blob = await res.blob();
      setReceiptSlideOver((prev) => prev ? { ...prev, blobUrl: URL.createObjectURL(blob), loading: false } : null);
    } catch {
      setReceiptSlideOver((prev) => prev ? { ...prev, loading: false, error: true } : null);
    }
  };

  // -- Fetch month report --
  const { data, isLoading, error } = useQuery<MonthReportResponse>({
    queryKey: ["reports", year, month],
    queryFn: () => api.get(`/api/reports/${year}/${month}`),
  });

  // -- Share mutation --
  const shareMutation = useMutation<ShareLinkResponse>({
    mutationFn: () => api.post(`/api/reports/${year}/${month}/share`),
    onSuccess: (resp) => {
      setShareUrl(resp.url);
    },
  });

  // -- Loading --
  if (isLoading) {
    return <AccountDetailSkeleton />;
  }

  // -- Error --
  if (error || !data) {
    return (
      <div className="space-y-4 pt-4">
        <ErrorDisplay error={error} context="report" />
      </div>
    );
  }

  const accounts = data.accounts ?? [];
  const defaultTab = accounts.length > 0 ? accounts[0].account : "";

  // Share of transactions with a matched document — drives the coverage badge.
  const allTxns = accounts.flatMap(a => a.transactions);
  const matchedCount = allTxns.filter(t =>
    ["MATCHED", "LINKED", "IGNORED"].includes(t.status)
  ).length;
  const matchRate = allTxns.length > 0
    ? Math.round((matchedCount / allTxns.length) * 100)
    : 0;

  // Map each transaction id → the account tab it belongs to so a click on a
  // "Linked to…" line can switch tabs when the counterpart lives elsewhere.
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
    flashRowWhenMounted(`txn-${targetId}`);
  }

  return (
    <div className="space-y-6 pt-4">
      {/* Inline CSS: row highlight animation for "Linked to…" click-jumps */}
      <style>{HIGHLIGHT_ROW_CSS}</style>
      {/* Print stylesheet */}
      <style>{`
        @media print {
          .no-print, nav, header, [data-sidebar], button:not(.print-keep) {
            display: none !important;
          }
          body { background: white !important; }
          * { box-shadow: none !important; border-radius: 0 !important; }
          .detail-tx-row td { border: 1px solid #ddd !important; padding: 6px 8px !important; }
          table { border-collapse: collapse; width: 100%; }
        }
      `}</style>

      {/* Navigation + header */}
      <div className="flex items-start justify-between">
        <div className="space-y-1">
          {data.company_name && (
            <p className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              {data.company_name}
            </p>
          )}
          <div className="flex items-center gap-3">
            <h2 className="text-3xl font-bold tracking-tight">
              {monthName(year, month)} {year}
            </h2>
            <DocumentCoverageBadge matchRate={matchRate} />
          </div>
          <p className="text-muted-foreground">
            <span className="font-mono">{data.total_transactions}</span> transaction
            {data.total_transactions !== 1 ? "s" : ""} across{" "}
            <span className="font-mono">{accounts.length}</span> account{accounts.length !== 1 ? "s" : ""}
          </p>
        </div>
      </div>

      {/* Action buttons */}
      <div className="flex flex-wrap items-center gap-2">
        <ExportDropdown year={year} month={month} />

        <Button
          variant="outline"
          size="sm"
          className="gap-1.5 border-primary/20 text-primary hover:bg-primary/5"
          disabled={shareMutation.isPending}
          onClick={() => shareMutation.mutate()}
        >
          {shareMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Link2 className="h-4 w-4" />
          )}
          Share
        </Button>

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

      {/* Share link banner */}
      {shareUrl && (
        <ShareBanner
          shareUrl={shareUrl}
          onDismiss={() => setShareUrl(null)}
        />
      )}

      {/* Account tabs with transaction tables */}
      {accounts.length === 0 ? (
        <Card className="shadow-warm rounded-xl">
          <CardContent className="flex h-[300px] items-center justify-center">
            <div className="text-center space-y-2">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-muted">
                <FileText className="h-6 w-6 text-muted-foreground/40" />
              </div>
              <p className="text-sm text-muted-foreground">
                No transactions for {monthName(year, month)} {year}.
              </p>
            </div>
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
                <CardHeader className="pb-0">
                  <CardTitle className="text-lg font-bold">{acct.account}</CardTitle>
                </CardHeader>
                <CardContent className="p-0 pt-4">
                  <AccountTransactionTable
                    account={acct}
                    widths={widths}
                    beginResize={beginResize}
                    onJumpToLinked={handleJumpToLinked}
                    onViewReceipt={handleViewReceipt}
                  />
                </CardContent>
                <CardFooter className="border-t px-4 py-3 bg-muted/20">
                  <AccountSummary account={acct} />
                </CardFooter>
              </Card>
            </TabsContent>
          ))}
        </Tabs>
      )}

      {/* Receipt slide-over panel */}
      <Sheet
        open={receiptSlideOver !== null}
        onOpenChange={(open) => {
          if (!open) {
            if (receiptSlideOver?.blobUrl) URL.revokeObjectURL(receiptSlideOver.blobUrl);
            setReceiptSlideOver(null);
          }
        }}
      >
        <SheetContent className="sm:max-w-lg p-0 flex flex-col">
          <SheetHeader className="px-6 pt-6 pb-4 border-b">
            <SheetTitle className="text-base font-semibold truncate">
              {receiptSlideOver?.description ?? "Receipt"}
            </SheetTitle>
          </SheetHeader>
          <div className="flex-1 overflow-auto p-4">
            {receiptSlideOver?.loading && (
              <div className="flex items-center justify-center h-64">
                <Loader2 className="h-6 w-6 animate-spin text-primary" />
              </div>
            )}
            {receiptSlideOver?.error && (
              <div className="flex flex-col items-center justify-center h-64 text-center">
                <FileText className="h-8 w-8 text-muted-foreground/40 mb-2" />
                <p className="text-sm text-muted-foreground">Could not load receipt preview</p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => receiptSlideOver && handleViewReceipt(receiptSlideOver.docId, receiptSlideOver.description)}
                >
                  Try again
                </Button>
              </div>
            )}
            {receiptSlideOver?.blobUrl && (
              <div className="space-y-3">
                {receiptSlideOver.blobUrl.includes("pdf") ? (
                  <iframe
                    src={receiptSlideOver.blobUrl}
                    className="w-full h-[70vh] rounded-lg border"
                    title="Receipt preview"
                  />
                ) : (
                  <img
                    src={receiptSlideOver.blobUrl}
                    alt="Receipt"
                    className="w-full rounded-lg border"
                  />
                )}
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  onClick={() => {
                    if (receiptSlideOver?.blobUrl) {
                      window.open(receiptSlideOver.blobUrl, "_blank", "noopener,noreferrer");
                    }
                  }}
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  Open in new tab
                </Button>
              </div>
            )}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
