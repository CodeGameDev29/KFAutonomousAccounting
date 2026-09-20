/** Shared types for report components. */

/**
 * Summary of the transaction on the other side of a link (credit-card payment,
 * Wise transfer, refund, etc.). Included when the counterpart is in the same
 * month's batch so the UI can render an inline reference.
 */
export interface LinkedTransactionSummary {
  id: number;
  account: string;
  date: string;
  description: string;
  amount: string;
  currency: string;
  type: "CREDIT" | "DEBIT";
  link_type: string;
}

/** Types for the authenticated AccountDetail view (GET /api/reports/{year}/{month}). */

export type TransactionStatus =
  | "MATCHED"
  | "UNMATCHED"
  | "PENDING_REVIEW"
  /** Self-evident — needs no receipt or note. The amount STILL COUNTS. */
  | "IGNORED"
  | "LINKED"
  /** Not a real transaction (rejected at import). The amount counts nowhere. */
  | "EXCLUDED";

export interface MonthTransaction {
  id: number;
  date: string;
  description: string;
  amount: string;
  currency: string;
  type: "CREDIT" | "DEBIT";
  status: TransactionStatus;
  has_receipt: boolean;
  receipt_path?: string | null;
  receipt_doc_id: number | null;
  vendor: string | null;
  category: string | null;
  /** First counterpart — kept for callers written against the 1:1 shape. */
  linked_to: LinkedTransactionSummary | null;
  /** Every counterpart. A transaction may be linked to several. */
  linked_to_all?: LinkedTransactionSummary[];
  match_score?: number | null;
  match_explanation?: string | null;
  match_sub_scores?: {
    amount_score: number | null;
    date_score: number | null;
    vendor_score: number | null;
  } | null;
}

export interface AccountCurrencyTotal {
  currency: string;
  income: number;
  expenses: number;
  net: number;
}

export interface MonthAccount {
  account: string;
  transaction_count: number;
  /** Cross-currency sums — legacy only; render currency_totals instead. */
  income: number;
  expenses: number;
  /** Per-currency totals (transfers excluded). CAD first, then activity. */
  currency_totals?: AccountCurrencyTotal[];
  transactions: MonthTransaction[];
}

/** Shared report (public) uses the same shape as the authenticated month view. */
export type Transaction = MonthTransaction;
export type AccountGroup = MonthAccount;

export interface MonthDetailResponse {
  label: string;
  total_transactions: number;
  accounts: AccountGroup[];
}

export interface SharedReceiptResponse {
  url: string;
}

export interface MonthReportResponse {
  year: number;
  month: number;
  label: string;
  total_transactions: number;
  company_name: string | null;
  closed: boolean;
  closed_at: string | null;
  accounts: MonthAccount[];
}

export interface ShareLinkResponse {
  token: string;
  expires_at: string;
  url: string;
}

export interface CloseMonthResponse {
  status: string;
  year: number;
  month: number;
  label: string;
  closed: boolean;
  closed_at: string;
  transaction_count: number;
  resolved_count: number;
  resolution_rate: number;
  unresolved_count: number;
}

export interface ReopenMonthResponse {
  status: string;
  year: number;
  month: number;
  label: string;
  closed: boolean;
  reopened_at: string;
}

export interface PdfExportResponse {
  status: string;
  url: string;
  filename: string;
}

export interface BinderExportResponse {
  status: string;
  url: string;
  filename: string;
}
