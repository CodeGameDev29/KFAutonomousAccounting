/**
 * Wording for the processing queue. Kept out of the components so the panel
 * and the header indicator never disagree about what a state is called.
 *
 * House style.
 * calm, plain, no exclamation marks, no jargon, and never a number the user
 * cannot act on.
 */
import type { JobKind, QueueRow, RowStatus, JobSummary } from "@/stores/processing-queue-store";

/** "about 22 min left" — deliberately vague, because the estimate is. */
export function formatEta(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds < 45) return "under a minute left";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${Math.max(1, minutes)} min left`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (rest === 0) return `about ${hours} h left`;
  return `about ${hours} h ${rest} min left`;
}

/**
 * How old the numbers on screen are: "last known 12 s ago".
 *
 * Shown whenever live updates are paused or the last poll failed. The point is
 * the opposite of reassurance — it says plainly that nothing here has been
 * confirmed since then, so a restart or an outage never leaves the strip
 * asserting "4 running, about 12 min left" as if it were happening now.
 */
export function formatLastKnown(ageMs: number): string {
  const seconds = Math.max(0, Math.round(ageMs / 1000));
  if (seconds < 90) return `last known ${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `last known ${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0
    ? `last known ${hours} h ago`
    : `last known ${hours} h ${rest} min ago`;
}

/** Elapsed clock for a row: "4s", "1m 20s", "12m". */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 10) return `${minutes}m ${seconds}s`;
  return `${minutes}m`;
}

/**
 * Milliseconds this row has been where it is.
 *
 * A retried job keeps its original `created_at`, so a re-queued row anchored
 * there would report the age of its first attempt and read as a stall. For
 * anything still waiting, time in the queue runs from the last server touch;
 * for work in progress, from when it started.
 */
export function elapsedMsFor(row: QueueRow, now: number): number | null {
  const waiting = row.status === "queued" || row.status === "waiting_for_model";
  const anchor = waiting
    ? (row.updated_at ?? row.created_at ?? null)
    : (row.started_at ?? row.created_at ?? null);
  const base = anchor ? Date.parse(anchor) : row.seenAt;
  if (!Number.isFinite(base)) return null;
  const end = row.finished_at ? Date.parse(row.finished_at) : now;
  return Math.max(0, (Number.isFinite(end) ? end : now) - base);
}

/** Short label for the status pill. Always text — never colour alone. */
export const STATUS_LABELS: Record<RowStatus, string> = {
  uploading: "Uploading",
  queued: "Queued",
  running: "Extracting",
  waiting_for_model: "Waiting",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  duplicate: "Duplicate",
  upload_failed: "Not uploaded",
};

/**
 * The sentence under the filename: exactly where this file is right now.
 *   uploading            → "Uploading…"
 *   queued, 12 waiting   → "Queued · 12 ahead"
 *   queued, next in line → "Next up"
 *   running, one call    → "Reading 8 pages in one pass"
 *   running, paged       → "Extracting page 3 of 8"
 *   waiting_for_model    → "Waiting for the extraction model"
 *
 * A queued row reports `ahead` — how many of this user's uploads are still in
 * front of it — and never `position`, which is a submission sequence: a row
 * keeps the number it was given at submission long after everything before it
 * has finished, so it reads as work still to go. `position` stays in the
 * payload, for ordering and for the logs; it is not a number to show a person.
 */
export function stageText(row: QueueRow): string {
  switch (row.status) {
    case "uploading":
      return "Uploading…";
    case "queued":
      if (row.ahead != null) {
        return row.ahead > 0 ? `Queued · ${row.ahead} ahead` : "Next up";
      }
      return "Queued";
    case "running": {
      const total = row.pages_total ?? 0;
      const generic =
        row.kind === "statement" ? "Reading the statement" : "Reading the receipt";
      if (total <= 1) return generic;
      // One LLM call over the whole document: a page counter would sit at the
      // first page until the call returns and then jump to the total, so say
      // what is actually happening and let the elapsed clock carry the movement.
      if (row.result?.mode === "one_pass") {
        return `Reading ${total} pages in one pass`;
      }
      // Real page batches: the counter is true, so show it.
      if (row.result?.mode === "paged" || row.pages_done) {
        const page = Math.min(total, Math.max(1, (row.pages_done ?? 0) + 1));
        return `Extracting page ${page} of ${total}`;
      }
      return generic;
    }
    case "waiting_for_model":
      return "Waiting for the extraction model";
    case "done":
      return resultSummary(row) ?? "Done";
    case "failed":
      return row.error_message || row.error_code || "Extraction failed";
    case "cancelled":
      return "Cancelled";
    case "duplicate":
      return duplicateText(row);
    case "upload_failed":
      return row.error_message || "Upload failed";
    default:
      return "";
  }
}

export function duplicateText(row: QueueRow): string {
  const prior = row.duplicateOf?.prior_filename;
  const when = row.duplicateOf?.processed_at;
  let text = "Already in your books";
  if (prior && prior !== row.filename) text += ` as “${prior}”`;
  if (when) {
    const d = new Date(when);
    if (!Number.isNaN(d.getTime())) text += ` (${d.toLocaleDateString("en-CA")})`;
  }
  return text;
}

/**
 * Every money amount leaves the API as a JSON number now (`_total_number` in
 * server/api/receipts.py), but the coercion stays: `String.prototype
 * .toLocaleString` silently ignores currency options, so a string that slipped
 * through — an older `processing_jobs.result` row, a hand-built payload — would
 * render a bare "41.25" with no dollar sign instead of failing loudly. Refuse to
 * print anything that cannot be parsed as a number.
 */
function money(total: number | string, currency?: string | null): string | null {
  const value = typeof total === "number" ? total : Number(total);
  if (!Number.isFinite(value)) return null;
  try {
    return value.toLocaleString("en-CA", {
      style: "currency",
      currency: currency || "CAD",
    });
  } catch {
    return `$${value.toFixed(2)}`;
  }
}

/** "Sample Street Cafe · $14.20" for a receipt, "8 rows imported" for a statement. */
export function resultSummary(row: QueueRow): string | null {
  const s: JobSummary | null | undefined = row.result?.summary;
  if (!s) return null;

  // `summary.text` is a pre-rendered line the backend also sends. It is the
  // fallback, not the first choice: it does not follow the house
  // separator/currency style, and every field it is built from is right here in
  // structured form. Used only when the structured fields say nothing. It is
  // composed in ASCII at the source (see `_receipt_summary`), so there is no
  // longer a mojibaked em dash to repair on this side.
  const parts: string[] = [];
  if (row.kind === "statement") {
    const rows = s.rows ?? s.imported;
    const dupes = s.duplicates ?? 0;
    // "0 rows imported" on a statement the books already hold reads like a
    // failure. Say what actually happened: every row was already present.
    if (!rows && dupes > 0) {
      parts.push(
        `Already in your books — ${dupes} row${dupes === 1 ? "" : "s"} already present, nothing new to import`,
      );
    } else if (rows != null) {
      parts.push(`${rows} row${rows === 1 ? "" : "s"} imported`);
      if (dupes > 0) parts.push(`${dupes} already present`);
    }
    if (s.account || s.institution) parts.push(String(s.account ?? s.institution));
    if (s.replaced) parts.push(`replaced ${s.replaced}`);
  } else {
    if (s.vendor) parts.push(s.vendor);
    if (s.total != null) {
      const amount = money(s.total, s.currency);
      if (amount) parts.push(amount);
    }
    if (s.date) parts.push(s.date);
    const rows = s.rows ?? s.imported;
    if (rows != null) parts.push(`${rows} row${rows === 1 ? "" : "s"} imported`);
  }
  if (parts.length === 0) return s.text || null;
  return parts.join(" · ");
}

/**
 * Where "View" goes. Both land on Transactions, filtered to this file.
 *
 * The filename cannot be sent straight into `?statement=`. That key is the one
 * Transactions selects a **tab** by, and a tab exists only where
 * `transactions.source_file` does — that is, only if the statement imported at
 * least one row of its own. A statement whose every row was already in the books
 * imports none and has no tab, and the page would fall back to whichever tab
 * sorts first, opening some other statement's rows.
 *
 * `result.statement_tab` is the server's answer to "which tab holds this
 * statement's rows" — its own filename normally, the file that actually holds
 * them when it imported nothing, and absent when there is no tab to open. With
 * no tab, View goes to the statements list (`?statements=`) instead of to a lie;
 * `stageText` already says "Already in your books" next to it.
 */
export function viewHrefFor(row: QueueRow): string | null {
  const summary = row.result?.summary;
  if (row.kind === "statement") {
    // Read without widening JobSummary here: the field is the server's
    // (`_statement_summary` in server/api/transactions.py) and is also carried at
    // the top level of `result` for `GET /api/jobs` callers.
    const tab =
      (summary as { statement_tab?: string | null } | undefined)?.statement_tab ??
      (row.result as { statement_tab?: string | null } | undefined)?.statement_tab;
    if (tab) return `/transactions?statement=${encodeURIComponent(tab)}`;
    const sourceFile = summary?.source_file || row.filename;
    if (!sourceFile) return null;
    return `/transactions?statements=${encodeURIComponent(sourceFile)}`;
  }
  const needle = summary?.vendor || row.filename;
  if (!needle) return null;
  return `/transactions?ps=${encodeURIComponent(needle)}`;
}

export function kindLabel(kind: JobKind): string {
  return kind === "statement" ? "Statement" : "Receipt";
}

/**
 * The summary strip sentence:
 * "3 running · 41 queued · 2 waiting for model · 118 done · 1 failed · about 22 min left"
 *
 * Zero-valued segments are dropped so the strip never pads itself with noise;
 * if literally nothing is happening the caller shows the idle line instead.
 */
export function summaryStripText(
  counts: {
    uploading: number;
    running: number;
    queued: number;
    waiting_for_model: number;
    done: number;
    failed: number;
    cancelled: number;
    duplicate: number;
  },
  etaSeconds: number | null,
): string {
  const parts: string[] = [];
  if (counts.uploading) parts.push(`${counts.uploading} uploading`);
  if (counts.running) parts.push(`${counts.running} running`);
  if (counts.queued) parts.push(`${counts.queued} queued`);
  if (counts.waiting_for_model)
    parts.push(`${counts.waiting_for_model} waiting for model`);
  if (counts.done) parts.push(`${counts.done} done`);
  if (counts.duplicate) parts.push(`${counts.duplicate} already in your books`);
  if (counts.failed) parts.push(`${counts.failed} failed`);
  if (counts.cancelled) parts.push(`${counts.cancelled} cancelled`);
  const eta = formatEta(etaSeconds);
  if (eta) parts.push(eta);
  return parts.join(" · ");
}
