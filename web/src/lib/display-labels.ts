/** Maps match_type field values to user-friendly labels */
export const MATCH_METHOD_LABELS: Record<string, string> = {
  STRICT_PASS_1: "High confidence",
  STRICT_PASS_2: "Strong match",
  RELAXED: "Similar match",
  AGGRESSIVE: "Approximate match",
  LLM_LINK: "Smart link",
  MANUAL: "Manual match",
};

/** Maps parse method identifiers to user-friendly labels */
export const PARSE_METHOD_LABELS: Record<string, string> = {
  local_text: "AI Text Analysis",
  local_vision: "AI Document Scan",
  gemini_text: "AI Text Analysis",
  gemini_vision: "AI Document Scan",
  openai_fallback: "AI Processing (Advanced)",
  legacy_pdf_to_csv: "Standard Parser",
};

/** Maps status identifiers to user-friendly labels.
 *
 * IGNORED and EXCLUDED are opposites where money is concerned, and the label
 * has to carry that:
 *   IGNORED  — self-evident row (bank fee, interest, transfer). Needs no
 *              receipt and no note, but the amount STILL COUNTS everywhere.
 *              Never label this "Skipped" / "Ignored" / "Excluded": all three
 *              read as "this money doesn't count", which is false.
 *   EXCLUDED — the row isn't real (rejected at import). Amount counts nowhere.
 */
export const STATUS_LABELS: Record<string, string> = {
  IGNORED: "No receipt needed",
  EXCLUDED: "Excluded — not counted",
  PENDING_REVIEW: "Needs review",
  NEEDS_REVIEW: "Needs review",
  AUTO_APPROVED: "Auto-approved",
  USER_APPROVED: "Approved",
  USER_REJECTED: "Rejected",
  UNMATCHED: "Unmatched",
  MATCHED: "Matched",
  LINKED: "Linked",
};

/** Returns user-friendly label for any raw backend string. Falls back to title-casing. */
export function displayLabel(raw: string, dictionary?: Record<string, string>): string {
  if (dictionary && raw in dictionary) return dictionary[raw];
  if (raw in MATCH_METHOD_LABELS) return MATCH_METHOD_LABELS[raw];
  if (raw in STATUS_LABELS) return STATUS_LABELS[raw];
  if (raw in PARSE_METHOD_LABELS) return PARSE_METHOD_LABELS[raw];
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
