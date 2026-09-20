/** Types for cross-statement transaction linking feature. */

export interface TransactionLink {
  id: number;
  link_type: "CREDIT_CARD_PAYMENT" | "INTERNAL_TRANSFER" | "FX_CONVERSION" | "WISE_TRANSFER" | "REFUND" | "OTHER";
  confidence_score: number;
  status: string;
  match_source: string;
  explanation: string | null;
  direction: string;
  linked_txn_id: number;
  linked_account: string;
  linked_description: string;
  linked_amount: string;
  linked_currency: string;
  linked_date: string;
  linked_source_file: string;
  linked_status: string;
  linked_transaction_type: string;
  /** The link's source side — the anchor of a fan-out group. */
  anchor_transaction_id: number;
  /** Legs of one fan-out group share this. */
  chain_id: string | null;
  chain_position: number;
  exchange_rate: string | null;
  amount_variance: string | null;
  created_at: string | null;
}

/**
 * How much of a transaction its links account for.
 *
 * A split refund is only legible with the arithmetic attached: a -$480.00
 * charge with three credits linked to it is 56% refunded with $210.00 still
 * standing as expense. Only counterparts moving the opposite way, in the same
 * currency, are netted — cross-currency legs are real linkage but not a
 * computable offset, so they are reported separately instead of distorting
 * the ratio.
 */
export interface TransactionLinkSummary {
  link_count: number;
  /** Legs that actually contributed to offset_total. */
  offset_leg_count: number;
  chain_ids: string[];
  transaction_amount: string;
  transaction_currency: string;
  offset_total: string;
  net_remaining: string;
  offset_ratio: number | null;
  is_fully_offset: boolean | null;
  cross_currency_count: number;
  same_direction_count: number;
}

export interface TransactionLinksResponse {
  transaction_id: number;
  links: TransactionLink[];
  summary: TransactionLinkSummary;
}

export interface LinkedTransactionInfo {
  link_id: number;
  linked_txn_id: number;
  linked_account: string;
  linked_description: string;
  linked_amount: string;
  linked_currency: string;
  linked_source_file: string;
  link_type: string;
  confidence_score: number;
  link_status: string;
}

export interface ChainNode {
  transaction_id: number;
  depth: number;
  account: string;
  amount: string;
  currency: string;
  description: string;
  date: string;
  source_file: string;
  status: string;
  has_document_match: boolean;
  matched_doc_filename: string | null;
}

export interface TransactionChainResponse {
  nodes: ChainNode[];
  current_index: number;
}

export interface LinkCandidate {
  id: number;
  account: string;
  date: string;
  amount: string;
  currency: string;
  description: string;
  source_file: string;
  status: string;
  confidence_score: number;
  amount_score: number;
  date_score: number;
  keyword_score: number;
  account_pair_score: number;
  suggested_link_type: string;
  /**
   * Why this row cannot be selected, or null when it can.
   * "self" — it is the transaction being linked from.
   * "already_linked" — already linked to that transaction.
   * A search returns these flagged rather than omitting them, so an exact
   * id search that turns up nothing selectable still explains itself.
   */
  unavailable_reason: "self" | "already_linked" | null;
}

export interface CreateLinkRequest {
  source_transaction_id: number;
  /** Single-target form, kept for callers written against the 1:1 API. */
  target_transaction_id?: number;
  /** Fan-out form — every target joins one link group. */
  target_transaction_ids?: number[];
  link_type: string;
  explanation?: string;
}

export interface CreateLinkResponse {
  chain_id: string;
  link_type: string;
  status: string;
  source_transaction_id: number;
  created: number;
  links: Array<{
    link_id: number;
    source_transaction_id: number;
    target_transaction_id: number;
    chain_position: number;
  }>;
}

export interface ChainBreadcrumbEntry {
  transaction_id: number;
  account: string;
  amount: string;
  currency: string;
  source_file: string;
}

export interface UnifiedProof {
  proof_type: "document" | "transaction_link";
  proof_id: number;
  proof_label: string;
  confidence_score: number;
  status: string;
  match_source: string;
}
