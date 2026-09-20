/**
 * Shared search-matching helpers.
 *
 * Every search box in the app (server- or client-side) honours the same
 * "exhaust every criterion" contract: one input is tested against each possible
 * interpretation and matches if ANY of them hits, in priority order
 *   1. exact id   2. text substring   3. exact monetary value
 * rather than guessing a single interpretation. These helpers implement the
 * numeric arms client-side so they behave identically to the Postgres
 * predicates in db/database_pg.py (_search_id / _search_amount).
 */

// A monetary value the user might type: optional sign, thousands separators and
// a '$' are stripped before this test; a bare integer like "50" qualifies too.
const MONEY_RE = /^[-+]?\d+(?:\.\d+)?$/;

/**
 * The exact id a search targets, or null. Digits only; a leading '#' (rows
 * render as "#1234") and surrounding whitespace are tolerated.
 */
export function parseSearchId(search: string): number | null {
  const s = search.trim().replace(/^#/, "").trim();
  return /^\d+$/.test(s) ? Number(s) : null;
}

/**
 * The ABSOLUTE monetary value a search targets, or null. Tolerates a leading
 * '$', thousands separators and whitespace so "$1,234.50", "-12.50" and "50"
 * all parse. Callers compare against Math.abs(amount), so sign, format and
 * precision differences ("12.5" vs "12.50") all match.
 */
export function parseSearchAmount(search: string): number | null {
  const cleaned = search.trim().replace(/\$/g, "").replace(/,/g, "").replace(/\s/g, "");
  if (!cleaned || !MONEY_RE.test(cleaned)) return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? Math.abs(n) : null;
}

/**
 * True when `amount` equals the monetary value in `search`. Compared at cent
 * precision (abs) so "12.5" matches a 12.50 or -12.50 stored value and float
 * dust never hides an exact match.
 */
export function amountMatchesSearch(
  amount: number | string | null | undefined,
  search: string,
): boolean {
  const target = parseSearchAmount(search);
  if (target === null || amount == null) return false;
  const a = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(a)) return false;
  return Math.round(Math.abs(a) * 100) === Math.round(target * 100);
}

/**
 * Levenshtein distance, capped: returns `max + 1` as soon as it is exceeded.
 *
 * Bounded so a long haystack can't turn a keystroke into an O(n·m) scan of
 * every row.
 */
function boundedEditDistance(a: string, b: string, max: number): number {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const curr = [i];
    let rowMin = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost);
      if (curr[j] < rowMin) rowMin = curr[j];
    }
    if (rowMin > max) return max + 1;
    prev = curr;
  }
  return prev[b.length];
}

/** How many character errors a term of this length may contain and still match. */
function editBudget(term: string): number {
  if (term.length < 4) return 0;   // too short — everything would match
  if (term.length < 7) return 1;
  return 2;
}

/**
 * True when `query` appears in `haystack`, tolerating a character or two of
 * OCR error.
 *
 * A receipt's searchable text is whatever the vision model read off the image,
 * and handwriting is exactly where it slips: a photographed cheque payable to
 * JORDAN AVERY can come back as "KORDAN AVERY", so a search for the name printed
 * on the document finds nothing and the receipt looks like it was never uploaded.
 * Exact substring is tried first and still wins; the fuzzy pass only rescues
 * the cases a strict match drops.
 */
export function fuzzyTextMatch(haystack: string | null | undefined, query: string): boolean {
  const hay = (haystack ?? "").toLowerCase();
  const q = query.trim().toLowerCase();
  if (!q || !hay) return false;
  if (hay.includes(q)) return true;

  // BOTH sides are tokenised. Comparing a whole multi-word query against one
  // haystack word can never match — typing the payee exactly as printed,
  // "JORDAN AVERY", would be compared with the single token "kordan" and
  // rejected on length alone, so the very case this exists for would fail.
  const queryWords = q.split(/[^a-z0-9]+/).filter(Boolean);
  const hayWords = hay.split(/[^a-z0-9]+/).filter(Boolean);
  if (!queryWords.length || !hayWords.length) return false;

  // Every word of the query must find a home; a query word that is too short
  // to fuzz (under 4 chars) has to match some token exactly or by prefix.
  return queryWords.every((word) => {
    const budget = editBudget(word);
    return hayWords.some((hw) => {
      if (hw.includes(word)) return true;
      if (budget === 0) return false;
      if (boundedEditDistance(word, hw, budget) <= budget) return true;
      // Allow the query word to match the start of a longer token.
      return (
        hw.length > word.length &&
        boundedEditDistance(word, hw.slice(0, word.length), budget) <= budget
      );
    });
  });
}
