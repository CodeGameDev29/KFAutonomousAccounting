/**
 * The summary a reconciliation run returns, and how to say it out loud.
 *
 * `matched` is the number of matches the run WROTE. A pair that was already on
 * file and only got re-scored comes back as `refreshed` — it is not a new
 * match, and a run that only re-scores has `matched: 0`. Both the Transactions
 * run log and the Review completion toast read these, so they are formatted in
 * one place and the two pages cannot phrase the same run differently.
 */

export interface ReconcileRunResult {
  status:
    | "success"
    | "partial"
    | "no_matches"
    | "nothing_to_reconcile"
    | "no_data";
  /** Matches newly written by this run. */
  matched: number;
  /** Pairs already on file that this run re-scored in place — not new. */
  refreshed?: number;
  /** Pairs the engine put forward, before the storage rule decided. */
  proposed?: number;
  rate: number;
  remaining_transactions: number;
  remaining_documents: number;
  // Pairs the engine would not call a match: below the confidence floor, or a
  // receipt whose vendor contradicts the bank description. They wait in Review
  // and their transactions stay in the pool.
  flagged_for_review?: number;
  // Weak existing matches taken apart this run by a stronger candidate.
  displaced?: number;
  // How the AI agents fared. A fallback means an agent's LLM call failed and
  // deterministic scoring decided instead, which the run summary surfaces
  // rather than leaving it to the server log alone.
  llm_calls?: number;
  llm_fallbacks?: number;
}

/**
 * "3 new matches, 1 re-scored" — what the run actually did, in one phrase.
 * A second run over the same statement reads "0 new matches, 1 re-scored"
 * rather than claiming a match it did not create.
 */
export function summarizeMatchRun(result?: ReconcileRunResult | null): string {
  const matched = result?.matched ?? 0;
  const refreshed = result?.refreshed ?? 0;
  const parts = [`${matched} new match${matched === 1 ? "" : "es"}`];
  if (refreshed > 0) {
    parts.push(`${refreshed} re-scored`);
  }
  return parts.join(", ");
}
