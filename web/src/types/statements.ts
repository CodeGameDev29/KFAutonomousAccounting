export type StatementImportConfidence = "HIGH" | "MEDIUM" | "LOW" | "LEGACY";

export type ImportFlag =
  | "balance_mismatch"
  | "balance_out_of_tolerance"
  | "duplicate_suspected"
  | "date_out_of_period"
  | "amount_outlier"
  | "currency_mismatch"
  | "low_row_count"
  | "running_balance_inconsistent"
  | "skip_phrase_dropped"
  | "date_parse_error";

/** Parse lifecycle of a statement row. A PDF upload answers
 *  202 with a `processing` row; the background parse moves it to `imported`
 *  or `failed`. */
export type StatementStatus = "processing" | "imported" | "failed";

export interface Statement {
  id: string;
  user_id: string;
  source_file: string;
  status: StatementStatus;
  /** User-facing reason the parse failed; null otherwise. */
  status_message: string | null;
  /** Null while the parse is still running — nothing is known yet. */
  institution: string | null;
  account_type: string | null;
  account_number: string | null;
  currency: string | null;
  statement_period_start: string | null;
  statement_period_end: string | null;
  opening_balance: string | null;
  closing_balance: string | null;
  balance_matches: boolean | null;
  balance_difference: string | null;
  page_count: number | null;
  row_count: number | null;
  import_confidence: StatementImportConfidence;
  import_flags: ImportFlag[];
  parse_method:
    | "local_text"
    | "local_vision"
    | "gemini_text"
    | "gemini_vision"
    | "openai_fallback"
    | "legacy_pdf_to_csv";
  llm_cost_usd: number;
  created_at: string;
}

export interface StatementParseProgressEvent {
  source_file: string;
  current_page?: number;
  total_pages?: number;
  eta_seconds?: number | null;
}

export interface StatementParseCompleteEvent {
  source_file: string;
  imported: number;
  duplicates: number;
  batch_confidence: StatementImportConfidence;
  balance_matches: boolean | null;
  flagged_row_count: number;
  parse_method: string;
}

export interface StatementParseStartedEvent {
  source_file: string;
  page_count?: number;
}

export interface StatementParseErrorEvent {
  source_file: string;
  error: string;
  retry_allowed: boolean;
  statement_id?: string;
  error_code?: string;
  message?: string;
}

/** Pushed when a background statement parse reaches `imported`. Carries the
 *  same payload as `statement_parse_complete` plus the row count. */
export interface StatementParsedEvent extends StatementParseCompleteEvent {
  statement_id: string;
  row_count: number;
  status: "imported";
}

/** Pushed when a background statement parse reaches `failed`. `message` is the
 *  classified, user-facing reason. */
export interface StatementFailedEvent {
  statement_id: string;
  source_file: string;
  error: string;
  error_code: string;
  message: string;
  status: "failed";
  retry_allowed: boolean;
}
