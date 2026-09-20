/**
 * Processing queue — the single source of truth for "what is the server doing
 * with the files I just dropped".
 *
 * Why this exists: a bulk drop is many multi-page statements and many receipts,
 * and the extraction runs long after the bytes are up. A store that knew only
 * about the upload would have nothing left to say from the 202 onward, so a
 * long run would look like a hang. This store tracks the server-side job for
 * every file from the moment it is accepted until it is done, failed or
 * cancelled.
 *
 * Shape of the truth, in order of authority:
 *   1. `GET /api/jobs`  — the full picture; polled on page load and every 3s
 *                         while anything is in flight.
 *   2. SSE `job_updated` / `jobs_summary` — the fast path, same shapes.
 *   3. Local rows       — a file whose bytes are still going up has no job id
 *                         yet, so it gets a client-side row that is swapped
 *                         for the real job the moment the 202 lands.
 *
 * Nothing in here ever fails a file because of elapsed time. A job is failed
 * only when the server says it failed.
 */
import { useMemo } from "react";
import { create } from "zustand";
import { api, ApiError } from "@/lib/api";
import { queryClient } from "@/lib/query-client";

// ============================== Contract ==============================

export type JobKind = "receipt" | "statement";

/** Server-side job statuses, exactly as `GET /api/jobs` reports them. */
export type JobStatus =
  | "queued"
  | "running"
  | "waiting_for_model"
  | "done"
  | "failed"
  | "cancelled";

/** Client-only row statuses — a file that has no server job (yet, or ever). */
export type LocalStatus = "uploading" | "duplicate" | "upload_failed";

export type RowStatus = JobStatus | LocalStatus;

/** `result.summary` is deliberately loose: the engine fills in what it knows. */
export interface JobSummary {
  vendor?: string | null;
  date?: string | null;
  /** A JSON number, two decimal places, on every endpoint that sends one. */
  total?: number | null;
  currency?: string | null;
  confidence?: string | null;
  /** Statement: rows written to the ledger. `imported` is the legacy name. */
  rows?: number | null;
  imported?: number | null;
  duplicates?: number | null;
  replaced?: number | null;
  account?: string | null;
  institution?: string | null;
  source_file?: string | null;
  /** Pre-rendered one-liner, used verbatim when present. */
  text?: string | null;
}

export interface JobResult {
  document_id?: string | number | null;
  statement_id?: string | number | null;
  summary?: JobSummary | null;
  /** How the engine is reading this document, set the moment it starts:
   *  "one_pass" — the whole document goes up in a single LLM call, so nothing
   *  can advance a page counter until it answers; "paged" — real page batches,
   *  where "page 3 of 8" is true. Absent on an older backend. */
  mode?: "one_pass" | "paged" | string | null;
  /** Set by the auto upload zone's sniffer: this file was routed to the
   *  statement pipeline because it looks like a statement, not because the user
   *  said so. The row says so, and offers one click to undo it. */
  detected_as?: "statement" | string | null;
}

export interface Job {
  id: string;
  kind: JobKind;
  filename: string;
  status: JobStatus;
  /** Submission sequence for this user. Never shown to a person — see `ahead`. */
  position?: number | null;
  /** Queue depth: uploads of this user's still queued or waiting in front of
   *  this one. 0 means it is next. Null once it is no longer waiting. */
  ahead?: number | null;
  attempts?: number | null;
  pages_total?: number | null;
  pages_done?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  result?: JobResult | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string | null;
}

export interface JobCounts {
  queued: number;
  running: number;
  waiting_for_model: number;
  done: number;
  failed: number;
  cancelled: number;
}

export interface WorkerInfo {
  concurrency: number;
  /** Is the background worker thread actually running? False means uploads are
   *  still accepted and still queued, but nothing will ever pick them up until
   *  the stack restarts — the one queue state that cannot resolve itself, and
   *  so the one the panel has to say out loud. Absent on an older backend,
   *  which normalizes to true: never invent an outage. */
  alive: boolean;
  model_reachable: boolean;
  avg_seconds_receipt: number | null;
  avg_seconds_statement: number | null;
  eta_seconds: number | null;
}

/**
 * The run the user is watching, as the *server* defines it: every job created
 * since this user's queue was last empty (or since the last "Clear finished").
 *
 * A batch is a fact about the queue, not about one tab, so the queue answers it
 * (`GET /api/jobs` → `batch`, and the SSE `jobs_summary`). Counted in the
 * browser over the rows one tab happened to see go by, the progress would
 * restart at zero on every reload and two tabs watching the same import would
 * disagree.
 */
export interface BatchProgress {
  total: number;
  finished: number;
  started_at?: string | null;
}

export interface JobsResponse {
  jobs: Job[];
  counts?: Partial<JobCounts> | null;
  worker?: Partial<WorkerInfo> | null;
  batch?: BatchProgress | null;
}

/** What the panel renders: a job, or a local row that does not have one yet. */
export interface QueueRow extends Omit<Job, "status"> {
  status: RowStatus;
  /** True while this row exists only in the browser (bytes still uploading). */
  local?: boolean;
  /** Set on a `duplicate` row — what the server said about the prior copy. */
  duplicateOf?: {
    prior_filename?: string | null;
    processed_at?: string | null;
    document_id?: string | number | null;
    statement_id?: string | number | null;
    message?: string | null;
  } | null;
  /** Monotonic insertion counter; ties the newest-first ordering down. */
  seq: number;
  /** Wall-clock ms when this row was first seen, for elapsed time on locals. */
  seenAt: number;
  /** Held only while the row is local, so a failed upload can be retried
   *  without asking the user to find the file again. */
  file?: File;
  endpoint?: string;
}

export type ConnectionState = "live" | "polling" | "connecting";

const EMPTY_COUNTS: JobCounts = {
  queued: 0,
  running: 0,
  waiting_for_model: 0,
  done: 0,
  failed: 0,
  cancelled: 0,
};

const DEFAULT_WORKER: WorkerInfo = {
  concurrency: 1,
  alive: true,
  model_reachable: true,
  avg_seconds_receipt: null,
  avg_seconds_statement: null,
  eta_seconds: null,
};

export const ACTIVE_STATUSES: ReadonlySet<RowStatus> = new Set<RowStatus>([
  "uploading",
  "queued",
  "running",
  "waiting_for_model",
]);

export const TERMINAL_STATUSES: ReadonlySet<RowStatus> = new Set<RowStatus>([
  "done",
  "failed",
  "cancelled",
  "duplicate",
  "upload_failed",
]);

// The two halves of TERMINAL_STATUSES, because they clear differently: each
// half has its own button and its own server call. One button that dropped
// every terminal row locally while telling the server about `done` alone would
// make a failed row vanish and return on the next poll.

/** Rows that ended without an error. `duplicate` is client-only (the file was
 *  never queued) so it has no server row to delete. */
export const CLEARABLE_FINISHED: ReadonlySet<RowStatus> = new Set<RowStatus>([
  "done",
  "duplicate",
  "cancelled",
]);

/** Rows that ended badly. `upload_failed` is client-only — the bytes never
 *  reached the server, so there is no job for it. */
export const CLEARABLE_FAILED: ReadonlySet<RowStatus> = new Set<RowStatus>([
  "failed",
  "upload_failed",
]);

// ============================== Store =================================

interface ProcessingQueueState {
  rows: Record<string, QueueRow>;
  counts: JobCounts;
  worker: WorkerInfo;
  connection: ConnectionState;
  /** Set false the first time `GET /api/jobs` 404s — the backend is older than
   *  this UI. The panel then runs on locally-tracked rows only, and says so. */
  jobsApiAvailable: boolean;
  lastSyncAt: number | null;
  /** Last error from a jobs fetch that could not be explained away. */
  syncError: string | null;
  /** When the last poll failed, epoch ms. Null once one succeeds again. While
   *  this is set — or live updates are paused — every number on screen is a
   *  memory, and the panel says so rather than asserting it as current. */
  syncFailedAt: number | null;
  /** The batch as the server defines it. Null until the first answer, or on a
   *  backend that predates the field, in which case `batchIds` stands in. */
  batch: BatchProgress | null;
  /** Fallback batch: ids this tab watched go by. Only used when the server
   *  does not send `batch` — it cannot survive a reload or reach another tab. */
  batchIds: string[];
  busyIds: string[];

  // --- reducers -------------------------------------------------------
  applyJobs: (payload: JobsResponse) => void;
  applyJobUpdate: (job: Job) => void;
  applySummary: (
    counts: Partial<JobCounts>,
    worker?: Partial<WorkerInfo>,
    batch?: BatchProgress | null,
  ) => void;
  setConnection: (c: ConnectionState) => void;

  // --- local upload rows ---------------------------------------------
  beginUpload: (item: PendingUpload) => void;
  markUploadFailed: (tempId: string, message: string) => void;
  markDuplicate: (tempId: string, info: QueueRow["duplicateOf"]) => void;

  // --- actions --------------------------------------------------------
  uploadFiles: (items: { file: File; endpoint: string; kind: JobKind }[]) => void;
  addRejected: (files: { name: string }[], message: string, kind: JobKind) => void;
  retry: (id: string) => Promise<void>;
  cancel: (id: string) => Promise<void>;
  /** "No, it's a receipt": send an auto-detected statement back down the
   *  receipt pipeline. The file is already stored, so nothing is re-uploaded
   *  and the document is not sent to the model a second time. */
  rerouteToReceipt: (id: string) => Promise<void>;
  /** Clear the rows that ended cleanly (done, cancelled, duplicate). */
  clearFinished: () => Promise<void>;
  /** Clear the rows that failed. Separate from `clearFinished` because a
   *  failed row is the one a person most wants to keep on screen until they
   *  have read it. */
  clearFailed: () => Promise<void>;
  dismissRow: (id: string) => void;
}

let seqCounter = 0;
const nextSeq = () => ++seqCounter;

/** Everything about a job list that would change what the panel renders. */
let lastJobsSignature = "";
function jobsSignature(jobs: Job[]): string {
  return jobs
    .map(
      (j) =>
        // `error_message` is in here because it MOVES while the status does
        // not: a job climbing the timeout ladder stays `waiting_for_model` and
        // re-words its own row ("Attempt 2 of 3 timed out; retrying"). Without
        // it, that sentence would be dropped as "nothing changed" and the row
        // would sit on its first attempt for the length of the run.
        `${j.id}|${j.status}|${j.ahead ?? ""}|${j.pages_done ?? ""}|${j.pages_total ?? ""}|${j.attempts ?? ""}|${j.error_message ?? ""}|${j.result ? 1 : 0}|${j.updated_at ?? ""}`,
    )
    .join(";");
}

function normalizeCounts(c?: Partial<JobCounts> | null): JobCounts {
  return { ...EMPTY_COUNTS, ...(c ?? {}) };
}

/** A believable batch: two finite non-negative numbers, finished <= total. */
function normalizeBatch(b?: Partial<BatchProgress> | null): BatchProgress | null {
  if (!b) return null;
  const total = Number(b.total);
  const finished = Number(b.finished);
  if (!Number.isFinite(total) || total <= 0) return null;
  if (!Number.isFinite(finished) || finished < 0) return null;
  return {
    total,
    finished: Math.min(finished, total),
    started_at: b.started_at ?? null,
  };
}

function normalizeWorker(w?: Partial<WorkerInfo> | null): WorkerInfo {
  if (!w) return { ...DEFAULT_WORKER };
  return {
    concurrency: w.concurrency ?? DEFAULT_WORKER.concurrency,
    // Only an explicit `false` is an outage. A backend that does not send the
    // field, or sends null, must not make the panel announce a stopped worker.
    alive: w.alive !== false,
    model_reachable: w.model_reachable !== false,
    avg_seconds_receipt: w.avg_seconds_receipt ?? null,
    avg_seconds_statement: w.avg_seconds_statement ?? null,
    eta_seconds: w.eta_seconds ?? null,
  };
}

/** Merge a server job onto whatever row already exists for it. */
function mergeJob(prev: QueueRow | undefined, job: Job): QueueRow {
  return {
    ...(prev ?? {}),
    ...job,
    id: job.id,
    status: job.status,
    local: false,
    seq: prev?.seq ?? nextSeq(),
    seenAt: prev?.seenAt ?? Date.now(),
    // Keep an existing result/summary if this update omits it: the server may
    // push a lean `job_updated`, and the summary must not blink out.
    result: job.result ?? prev?.result ?? null,
  };
}

/**
 * Shared body of the two clear actions.
 *
 * `rowStatuses` is what leaves the local list at once, so the button visibly
 * does something. `serverStatuses` is what the server is then told to delete —
 * `DELETE /api/jobs` takes one finished status per call, and client-only rows
 * (`duplicate`, `upload_failed`) have no job to delete at all.
 *
 * Getting that pairing wrong is what makes a cleared row come back: drop a
 * failed row locally while asking the server for `done` alone, and the next
 * poll restores it.
 */
async function clearRows(
  rowStatuses: ReadonlySet<RowStatus>,
  serverStatuses: readonly ("done" | "failed" | "cancelled")[],
): Promise<void> {
  // Force the next poll to rebuild the list rather than short-circuit on an
  // unchanged signature: if a DELETE below fails, the rows must come back.
  lastJobsSignature = "";
  // Drop locally first so the list responds immediately, then tell the server.
  useProcessingQueue.setState((s) => {
    const rows: Record<string, QueueRow> = {};
    for (const [id, row] of Object.entries(s.rows)) {
      if (!rowStatuses.has(row.status)) rows[id] = row;
    }
    return {
      rows,
      batchIds: s.batchIds.filter((id) => rows[id] !== undefined),
    };
  });
  if (!useProcessingQueue.getState().jobsApiAvailable) return;
  for (const status of serverStatuses) {
    try {
      await api.delete(`/api/jobs?status=${status}`);
    } catch {
      // Best effort — the local list is already right, and the next poll
      // re-adds anything the server kept.
    }
  }
  void refreshJobs();
}

export const useProcessingQueue = create<ProcessingQueueState>((set, get) => ({
  rows: {},
  counts: { ...EMPTY_COUNTS },
  worker: { ...DEFAULT_WORKER },
  connection: "connecting",
  jobsApiAvailable: true,
  lastSyncAt: null,
  syncError: null,
  syncFailedAt: null,
  batch: null,
  batchIds: [],
  busyIds: [],

  applyJobs: (payload) =>
    set((s) => {
      // The idle heartbeat re-reads an unchanged list every 20s. Handing back
      // a fresh `rows` object each time would re-render the panel and the
      // header chip on every page, forever, for nothing.
      const signature = jobsSignature(payload.jobs ?? []);
      if (signature === lastJobsSignature && Object.keys(s.rows).length > 0) {
        return {
          counts: normalizeCounts(payload.counts),
          worker: normalizeWorker(payload.worker),
          batch: normalizeBatch(payload.batch) ?? s.batch,
          lastSyncAt: Date.now(),
          syncError: null,
          syncFailedAt: null,
        };
      }
      lastJobsSignature = signature;

      const rows = { ...s.rows };
      const batch = new Set(s.batchIds);
      const seenIds = new Set<string>();
      for (const job of payload.jobs ?? []) {
        if (!job || !job.id) continue;
        const id = String(job.id);
        seenIds.add(id);
        const merged = mergeJob(rows[id], { ...job, id });
        rows[id] = merged;
        if (ACTIVE_STATUSES.has(merged.status)) batch.add(id);
      }
      // A row the server no longer reports and that was not created locally
      // has been cleared elsewhere (another tab hit "Clear finished"). Drop it
      // so the two tabs agree. Local rows are never dropped this way.
      for (const id of Object.keys(rows)) {
        if (seenIds.has(id)) continue;
        if (rows[id].local) continue;
        delete rows[id];
        batch.delete(id);
      }
      return {
        rows,
        counts: normalizeCounts(payload.counts),
        worker: normalizeWorker(payload.worker),
        batch: normalizeBatch(payload.batch) ?? s.batch,
        batchIds: [...batch],
        lastSyncAt: Date.now(),
        syncError: null,
        syncFailedAt: null,
      };
    }),

  applyJobUpdate: (job) =>
    set((s) => {
      if (!job || !job.id) return s;
      const id = String(job.id);
      const prev = s.rows[id];
      const merged = mergeJob(prev, { ...job, id });
      const batch = new Set(s.batchIds);
      if (ACTIVE_STATUSES.has(merged.status)) batch.add(id);
      // Reaching a terminal state is the only moment the rest of the app needs
      // to re-fetch. Doing it on every progress tick would flush the whole
      // query cache a few hundred times per batch.
      if (
        prev &&
        !TERMINAL_STATUSES.has(prev.status) &&
        TERMINAL_STATUSES.has(merged.status)
      ) {
        invalidateListQueries();
      }
      return { rows: { ...s.rows, [id]: merged }, batchIds: [...batch] };
    }),

  applySummary: (counts, worker, batch) =>
    set((s) => ({
      counts: normalizeCounts({ ...s.counts, ...counts }),
      worker: worker ? normalizeWorker({ ...s.worker, ...worker }) : s.worker,
      batch: normalizeBatch(batch) ?? s.batch,
      // An event is as good as a poll: it proves the server is answering, so
      // the "last known" note clears on it too.
      lastSyncAt: Date.now(),
      syncFailedAt: null,
    })),

  setConnection: (c) => set({ connection: c }),

  beginUpload: ({ tempId, file, kind, endpoint }) =>
    set((s) => ({
      rows: {
        ...s.rows,
        [tempId]: {
          id: tempId,
          kind,
          filename: file.name,
          status: "uploading",
          local: true,
          seq: s.rows[tempId]?.seq ?? nextSeq(),
          seenAt: Date.now(),
          file,
          endpoint,
        },
      },
      batchIds: [...new Set([...s.batchIds, tempId])],
    })),

  markUploadFailed: (tempId, message) =>
    set((s) => {
      const prev = s.rows[tempId];
      if (!prev) return s;
      return {
        rows: {
          ...s.rows,
          [tempId]: { ...prev, status: "upload_failed", error_message: message },
        },
      };
    }),

  markDuplicate: (tempId, info) =>
    set((s) => {
      const prev = s.rows[tempId];
      if (!prev) return s;
      return {
        rows: {
          ...s.rows,
          [tempId]: { ...prev, status: "duplicate", duplicateOf: info ?? null },
        },
      };
    }),

  addRejected: (files, message, kind) =>
    set((s) => {
      const rows = { ...s.rows };
      for (const f of files) {
        const id = `rejected:${f.name}:${nextSeq()}`;
        rows[id] = {
          id,
          kind,
          filename: f.name,
          status: "upload_failed",
          error_message: message,
          local: true,
          seq: nextSeq(),
          seenAt: Date.now(),
        };
      }
      return { rows };
    }),

  uploadFiles: (items) => {
    for (const item of items) {
      const pending: PendingUpload = {
        ...item,
        tempId: `local:${nextSeq()}:${item.file.name}`,
      };
      get().beginUpload(pending);
      enqueueUpload(pending);
    }
    kickUploadWorkers();
  },

  retry: async (id) => {
    const row = get().rows[id];
    // A file that never reached the server is retried by uploading it again —
    // the File handle is held exactly so the user does not have to find it.
    if (row?.local) {
      if (!row.file || !row.endpoint) return;
      const pending: PendingUpload = {
        file: row.file,
        endpoint: row.endpoint,
        kind: row.kind,
        tempId: row.id,
      };
      get().beginUpload(pending);
      enqueueUpload(pending);
      kickUploadWorkers();
      return;
    }
    if (get().busyIds.includes(id)) return;
    set((s) => ({ busyIds: [...s.busyIds, id] }));
    try {
      const job = await api.post<Job>(`/api/jobs/${id}/retry`);
      if (job?.id) get().applyJobUpdate(job);
      else void refreshJobs();
    } catch (err) {
      set({ syncError: describeError(err) });
    } finally {
      set((s) => ({ busyIds: s.busyIds.filter((x) => x !== id) }));
      ensureSyncCadence();
    }
  },

  cancel: async (id) => {
    const row = get().rows[id];
    // A local row has no server job to cancel — the fetch is already in flight
    // and aborting it mid-stream would leave a half-written file on disk. Just
    // take the row off the list.
    if (row?.local) {
      get().dismissRow(id);
      return;
    }
    if (get().busyIds.includes(id)) return;
    set((s) => ({ busyIds: [...s.busyIds, id] }));
    try {
      const job = await api.post<Job>(`/api/jobs/${id}/cancel`);
      if (job?.id) get().applyJobUpdate(job);
      else void refreshJobs();
    } catch (err) {
      set({ syncError: describeError(err) });
    } finally {
      set((s) => ({ busyIds: s.busyIds.filter((x) => x !== id) }));
    }
  },

  rerouteToReceipt: async (id) => {
    const row = get().rows[id];
    // A local row never reached the server, so there is no job to re-point.
    if (!row || row.local) return;
    if (get().busyIds.includes(id)) return;
    set((s) => ({ busyIds: [...s.busyIds, id] }));
    try {
      const job = await api.post<Job & { replaced_job_id?: string }>(
        `/api/jobs/${id}/reroute`,
      );
      // The reroute replaces the job with a new one, so drop the old row rather
      // than leaving a ghost of one dropped file on the list twice.
      const replaced = job?.replaced_job_id ?? id;
      if (job?.id && String(job.id) !== replaced) get().dismissRow(replaced);
      if (job?.id) get().applyJobUpdate({ ...job, id: String(job.id) });
      else void refreshJobs();
    } catch (err) {
      set({ syncError: describeError(err) });
    } finally {
      set((s) => ({ busyIds: s.busyIds.filter((x) => x !== id) }));
      ensureSyncCadence();
    }
  },

  clearFinished: () => clearRows(CLEARABLE_FINISHED, ["done", "cancelled"]),

  clearFailed: () => clearRows(CLEARABLE_FAILED, ["failed"]),

  dismissRow: (id) =>
    set((s) => {
      const rows = { ...s.rows };
      delete rows[id];
      return { rows, batchIds: s.batchIds.filter((x) => x !== id) };
    }),
}));

// ============================== Selectors =============================
//
// Anything deriving a fresh array or object is exposed as a `use*` hook that
// memoizes on the raw state slices it reads. Passing a selector that allocates
// straight to `useProcessingQueue` would hand React a new reference on every
// render, which under zustand v5 / useSyncExternalStore is the "getSnapshot
// should be cached" render loop rather than a mere waste.

/**
 * The order of the list, and the whole reason the panel is readable.
 *
 * Page 1 must always show what is *moving*. Sorting "active first, then newest
 * first" does not do that: in a bulk drop every row is active and the newest
 * upload sorts above the jobs actually running, pushing them onto a later page
 * nobody looks at.
 *
 * So: running, then waiting for the model, then the queue in the order it will
 * actually drain (`ahead`, smallest first — "Next up" at the top), then bytes
 * still going up, then failures (the only thing a person can act on), then
 * everything finished, newest first.
 */
const ROW_ORDER: Record<RowStatus, number> = {
  running: 0,
  waiting_for_model: 1,
  queued: 2,
  uploading: 3,
  failed: 4,
  upload_failed: 4,
  cancelled: 5,
  duplicate: 5,
  done: 6,
};

function finishedAtMs(row: QueueRow): number {
  const raw = row.finished_at ? Date.parse(row.finished_at) : NaN;
  return Number.isFinite(raw) ? raw : row.seenAt;
}

function startedAtMs(row: QueueRow): number {
  const raw = row.started_at ? Date.parse(row.started_at) : NaN;
  return Number.isFinite(raw) ? raw : row.seenAt;
}

export function selectRows(s: ProcessingQueueState): QueueRow[] {
  const rows = Object.values(s.rows);
  rows.sort((a, b) => {
    const ra = ROW_ORDER[a.status] ?? 9;
    const rb = ROW_ORDER[b.status] ?? 9;
    if (ra !== rb) return ra - rb;

    // Running and waiting: longest-running first. Stable on purpose — a row
    // that finishes leaves the group and the next one appends at the bottom,
    // so the top of the list does not reshuffle every three seconds.
    if (ra <= 1) {
      const diff = startedAtMs(a) - startedAtMs(b);
      if (diff !== 0) return diff;
    }

    // Queued: the order the worker will actually take them in.
    if (ra === 2) {
      const aa = a.ahead ?? a.position ?? Number.MAX_SAFE_INTEGER;
      const ba = b.ahead ?? b.position ?? Number.MAX_SAFE_INTEGER;
      if (aa !== ba) return aa - ba;
      return a.seq - b.seq;
    }

    // Finished: newest first, because the interesting one is the one that just
    // landed.
    if (ra >= 4) {
      const diff = finishedAtMs(b) - finishedAtMs(a);
      if (diff !== 0) return diff;
    }

    return b.seq - a.seq;
  });
  return rows;
}

/**
 * Counts the panel shows. The server's `counts` object is authoritative for
 * server-side work; local rows are added on top so a file whose bytes are
 * still going up is visible in the same strip.
 */
export interface DisplayCounts extends JobCounts {
  uploading: number;
  duplicate: number;
  total: number;
  active: number;
}

export function selectDisplayCounts(s: ProcessingQueueState): DisplayCounts {
  const local = { uploading: 0, duplicate: 0, upload_failed: 0 };
  const derived = { ...EMPTY_COUNTS };
  for (const row of Object.values(s.rows)) {
    switch (row.status) {
      case "uploading":
        local.uploading++;
        break;
      case "duplicate":
        local.duplicate++;
        break;
      case "upload_failed":
        local.upload_failed++;
        break;
      default:
        derived[row.status]++;
    }
  }
  // Trust the server's counts when it is answering; otherwise count the rows
  // on screen. Either way the local upload/duplicate rows are added on top.
  const base = s.jobsApiAvailable && s.lastSyncAt ? s.counts : derived;
  const counts: JobCounts = {
    queued: base.queued,
    running: base.running,
    waiting_for_model: base.waiting_for_model,
    done: base.done,
    failed: base.failed + local.upload_failed,
    cancelled: base.cancelled,
  };
  const active = local.uploading + counts.queued + counts.running + counts.waiting_for_model;
  return {
    ...counts,
    uploading: local.uploading,
    duplicate: local.duplicate,
    active,
    total:
      active + counts.done + counts.failed + counts.cancelled + local.duplicate,
  };
}

/**
 * done/total over the batch the user is watching.
 *
 * The server's `batch` wins whenever it is there: it is computed from the queue
 * itself, so it survives a reload, a second tab and a restart. The tab-local
 * count over `batchIds` is the fallback for a backend that does not send one,
 * and it resets whenever this tab reloads.
 */
export function selectBatchProgress(s: ProcessingQueueState): {
  done: number;
  total: number;
  percent: number;
  fromServer: boolean;
} {
  if (s.batch && s.batch.total > 0) {
    const { finished, total } = s.batch;
    return {
      done: finished,
      total,
      percent: Math.round((finished / total) * 100),
      fromServer: true,
    };
  }
  let done = 0;
  let total = 0;
  for (const id of s.batchIds) {
    const row = s.rows[id];
    if (!row) continue;
    total++;
    if (TERMINAL_STATUSES.has(row.status)) done++;
  }
  return {
    done,
    total,
    percent: total > 0 ? Math.round((done / total) * 100) : 0,
    fromServer: false,
  };
}

/**
 * Is what the panel is showing current, or a memory?
 *
 * True when live updates are paused or the last poll failed — a restart, a
 * network blip, a sleeping machine. The panel then mutes every count and says
 * how old they are, because "4 running · about 12 min left" stated flatly while
 * the server is down is simply a lie, and the one thing this panel must never do
 * is assert a stale fact as current.
 */
export function selectStale(s: ProcessingQueueState): boolean {
  return s.syncFailedAt !== null || s.connection === "polling";
}

/** True while anything at all is in flight — drives the header indicator. */
export function selectInFlight(s: ProcessingQueueState): boolean {
  if (Object.values(s.rows).some((r) => ACTIVE_STATUSES.has(r.status))) return true;
  if (!s.jobsApiAvailable || !s.lastSyncAt) return false;
  const { queued, running, waiting_for_model } = s.counts;
  return queued + running + waiting_for_model > 0;
}

/**
 * Seconds of work left, best estimate. `worker.eta_seconds` wins when the
 * server offers one; otherwise derive it from the averages it reports.
 */
export function selectEtaSeconds(s: ProcessingQueueState): number | null {
  if (s.worker.eta_seconds != null && s.worker.eta_seconds > 0) {
    return s.worker.eta_seconds;
  }
  const avgReceipt = s.worker.avg_seconds_receipt;
  const avgStatement = s.worker.avg_seconds_statement;
  if (!avgReceipt && !avgStatement) return null;
  let seconds = 0;
  for (const row of Object.values(s.rows)) {
    if (!ACTIVE_STATUSES.has(row.status)) continue;
    const avg =
      row.kind === "statement"
        ? (avgStatement ?? avgReceipt ?? 0)
        : (avgReceipt ?? avgStatement ?? 0);
    if (row.status === "running" && row.pages_total && row.pages_done != null) {
      const remaining = Math.max(0, row.pages_total - row.pages_done);
      seconds += (avg * remaining) / Math.max(1, row.pages_total);
    } else {
      seconds += avg;
    }
  }
  if (seconds <= 0) return null;
  return Math.round(seconds / Math.max(1, s.worker.concurrency));
}

// ---------------------------- Selector hooks ---------------------------

export function useQueueRows(): QueueRow[] {
  const rows = useProcessingQueue((s) => s.rows);
  return useMemo(() => selectRows({ rows } as ProcessingQueueState), [rows]);
}

export function useDisplayCounts(): DisplayCounts {
  const rows = useProcessingQueue((s) => s.rows);
  const counts = useProcessingQueue((s) => s.counts);
  const jobsApiAvailable = useProcessingQueue((s) => s.jobsApiAvailable);
  const lastSyncAt = useProcessingQueue((s) => s.lastSyncAt);
  return useMemo(
    () =>
      selectDisplayCounts({
        rows,
        counts,
        jobsApiAvailable,
        lastSyncAt,
      } as ProcessingQueueState),
    [rows, counts, jobsApiAvailable, lastSyncAt],
  );
}

export function useBatchProgress(): {
  done: number;
  total: number;
  percent: number;
  fromServer: boolean;
} {
  const rows = useProcessingQueue((s) => s.rows);
  const batchIds = useProcessingQueue((s) => s.batchIds);
  const batch = useProcessingQueue((s) => s.batch);
  return useMemo(
    () => selectBatchProgress({ rows, batchIds, batch } as ProcessingQueueState),
    [rows, batchIds, batch],
  );
}

/** `{ stale, lastSyncAt }` — whether the numbers are current, and how old. */
export function useFreshness(): { stale: boolean; lastSyncAt: number | null } {
  const connection = useProcessingQueue((s) => s.connection);
  const syncFailedAt = useProcessingQueue((s) => s.syncFailedAt);
  const lastSyncAt = useProcessingQueue((s) => s.lastSyncAt);
  return useMemo(
    () => ({
      stale: selectStale({ connection, syncFailedAt } as ProcessingQueueState),
      lastSyncAt,
    }),
    [connection, syncFailedAt, lastSyncAt],
  );
}

export function useEtaSeconds(): number | null {
  const rows = useProcessingQueue((s) => s.rows);
  const worker = useProcessingQueue((s) => s.worker);
  return useMemo(
    () => selectEtaSeconds({ rows, worker } as ProcessingQueueState),
    [rows, worker],
  );
}

// ======================= Upload orchestration =========================
// Bytes still have to go up the wire, and three concurrent uploads is what a
// modest link carries without starving the rest of the page. What matters is
// what happens afterwards: a 202 hands the file off to the server-side queue
// and this worker moves on. Nothing here is ever timed out.

const MAX_CONCURRENT_UPLOADS = 3;

interface PendingUpload {
  file: File;
  endpoint: string;
  kind: JobKind;
  tempId: string;
}

const uploadQueue: PendingUpload[] = [];
let activeUploads = 0;

function enqueueUpload(item: PendingUpload) {
  uploadQueue.push(item);
}

function kickUploadWorkers() {
  while (activeUploads < MAX_CONCURRENT_UPLOADS && uploadQueue.length > 0) {
    activeUploads++;
    void runUploadWorker();
  }
  ensureSyncCadence();
}

async function runUploadWorker() {
  try {
    for (;;) {
      const item = uploadQueue.shift();
      if (!item) return;
      await uploadOne(item);
    }
  } finally {
    activeUploads--;
    ensureSyncCadence();
  }
}

/**
 * Normalize an upload response into a job row.
 *
 * Two shapes are accepted on purpose:
 *  - The queue contract: `{job_id, kind, filename, status, position}` (202),
 *    and a completed job object for a CSV/XLSX statement parsed inline.
 *  - The shape the API answered with before the queue existed:
 *    `202 {status:"processing", statement_id}` for a statement PDF, and
 *    `200 {status:"ok", doc_id, vendor, total, ...}` for a receipt. Keeping
 *    this path means uploads keep working while the backend catches up.
 */
interface RawUploadResponse {
  job_id?: string | number;
  id?: string | number;
  kind?: JobKind;
  filename?: string;
  status?: string;
  position?: number;
  ahead?: number | null;
  pages_total?: number;
  pages_done?: number;
  result?: JobResult;
  duplicate?: boolean;
  // legacy
  doc_id?: string;
  statement_id?: string | null;
  vendor?: string;
  date?: string;
  total?: number;
  currency?: string;
  confidence?: string;
  imported?: number;
  duplicates?: number;
  replaced?: number;
  account?: string;
  source_file?: string;
  prior_filename?: string;
  processed_at?: string;
  message?: string;
}

const SERVER_JOB_STATUSES = new Set<string>([
  "queued",
  "running",
  "waiting_for_model",
  "done",
  "failed",
  "cancelled",
]);

function legacySummary(raw: RawUploadResponse): JobSummary {
  return {
    vendor: raw.vendor ?? null,
    date: raw.date ?? null,
    total: raw.total ?? null,
    currency: raw.currency ?? null,
    confidence: raw.confidence ?? null,
    rows: raw.imported ?? null,
    imported: raw.imported ?? null,
    duplicates: raw.duplicates ?? null,
    replaced: raw.replaced ?? null,
    account: raw.account ?? null,
    source_file: raw.source_file ?? null,
  };
}

async function uploadOne(item: PendingUpload) {
  const store = useProcessingQueue.getState();
  try {
    const formData = new FormData();
    formData.append("file", item.file);
    const raw = await api.upload<RawUploadResponse>(item.endpoint, formData);

    if (raw?.duplicate) {
      store.markDuplicate(item.tempId, {
        prior_filename: raw.prior_filename ?? null,
        processed_at: raw.processed_at ?? null,
        document_id: raw.result?.document_id ?? raw.doc_id ?? null,
        statement_id: raw.result?.statement_id ?? raw.statement_id ?? null,
        message: raw.message ?? null,
      });
      return;
    }

    const jobId = raw?.job_id ?? raw?.id;
    if (jobId != null) {
      const status = SERVER_JOB_STATUSES.has(String(raw.status))
        ? (raw.status as JobStatus)
        : "queued";
      // Swap the local row for the real job: same place in the list, same
      // elapsed clock, but now addressable by retry / cancel / view.
      swapLocalForJob(item.tempId, {
        id: String(jobId),
        kind: raw.kind ?? item.kind,
        filename: raw.filename ?? item.file.name,
        status,
        position: raw.position ?? null,
        ahead: raw.ahead ?? null,
        pages_total: raw.pages_total ?? null,
        pages_done: raw.pages_done ?? null,
        result: raw.result ?? null,
      });
      ensureSyncCadence();
      return;
    }

    // --- legacy shapes -------------------------------------------------
    if (raw?.status === "processing" && raw.statement_id) {
      const id = `statement:${raw.statement_id}`;
      swapLocalForJob(item.tempId, {
        id,
        kind: "statement",
        filename: item.file.name,
        status: "running",
      });
      trackLegacyStatement(String(raw.statement_id), id, item.file.name);
      return;
    }

    swapLocalForJob(item.tempId, {
      id: `done:${raw?.doc_id ?? item.tempId}`,
      kind: item.kind,
      filename: item.file.name,
      status: "done",
      finished_at: new Date().toISOString(),
      result: {
        document_id: raw?.doc_id ?? null,
        statement_id: raw?.statement_id ?? null,
        summary: legacySummary(raw ?? {}),
      },
    });
    invalidateListQueries();
  } catch (err) {
    // The pre-queue endpoints answer 409 for a duplicate; the queue contract
    // answers 200 {duplicate:true}. Both mean the same thing to the user.
    if (err instanceof ApiError && err.status === 409) {
      let info: QueueRow["duplicateOf"] = null;
      try {
        info = JSON.parse(err.detail) as QueueRow["duplicateOf"];
      } catch {
        info = { message: err.detail };
      }
      store.markDuplicate(item.tempId, info);
      return;
    }
    store.markUploadFailed(item.tempId, describeError(err));
  }
}

/** Replace a local row with the server job, preserving list position. */
function swapLocalForJob(tempId: string, job: Job) {
  useProcessingQueue.setState((s) => {
    const local = s.rows[tempId];
    const rows = { ...s.rows };
    delete rows[tempId];
    const existing = rows[job.id];
    rows[job.id] = {
      ...(existing ?? {}),
      ...job,
      local: false,
      seq: local?.seq ?? existing?.seq ?? nextSeq(),
      seenAt: local?.seenAt ?? existing?.seenAt ?? Date.now(),
      // Let the File go: holding every handle would keep the whole drop in
      // memory for as long as the tab stays open.
      file: undefined,
      endpoint: undefined,
    };
    const batch = new Set(s.batchIds.filter((x) => x !== tempId));
    batch.add(job.id);
    return { rows, batchIds: [...batch] };
  });
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.detail || `Upload failed (${err.status}).`;
  if (err instanceof Error) return err.message;
  return String(err);
}

// ============ Legacy statement tracking (pre-queue backend) ============
// A backend that predates the job queue answers 202 {statement_id} for a
// statement PDF and finishes the parse in a background task. Where `GET
// /api/jobs` is missing, the terminal state comes from the statement SSE events
// and a 3 s statement poll instead. This block exists only for that case and
// can be dropped once no such backend is supported.

const legacyTracked = new Set<string>();

interface StatementRow {
  status?: string;
  status_message?: string | null;
  row_count?: number | null;
  institution?: string | null;
}

function trackLegacyStatement(statementId: string, rowId: string, fileName: string) {
  if (legacyTracked.has(statementId)) return;
  legacyTracked.add(statementId);

  let settled = false;
  let poll: ReturnType<typeof setInterval> | null = null;

  const cleanup = () => {
    legacyTracked.delete(statementId);
    window.removeEventListener("statement_parsed", onParsed);
    window.removeEventListener("statement_failed", onFailed);
    window.removeEventListener("statement_parse_progress", onProgress);
    if (poll) clearInterval(poll);
  };

  const finish = (job: Partial<Job>) => {
    if (settled) return;
    settled = true;
    cleanup();
    const current = useProcessingQueue.getState().rows[rowId];
    useProcessingQueue.getState().applyJobUpdate({
      id: rowId,
      kind: "statement",
      filename: current?.filename ?? fileName,
      status: "done",
      ...job,
    } as Job);
    invalidateListQueries();
  };

  function onParsed(ev: Event) {
    const d = (
      ev as CustomEvent<{
        statement_id?: string;
        imported?: number;
        duplicates?: number;
        account?: string;
        institution?: string;
      }>
    ).detail;
    if (String(d?.statement_id) !== statementId) return;
    finish({
      status: "done",
      finished_at: new Date().toISOString(),
      result: {
        statement_id: statementId,
        summary: {
          rows: d?.imported ?? null,
          duplicates: d?.duplicates ?? null,
          account: d?.account ?? d?.institution ?? null,
          source_file: fileName,
        },
      },
    });
  }

  function onFailed(ev: Event) {
    const d = (ev as CustomEvent<{ statement_id?: string; message?: string; error?: string }>).detail;
    if (String(d?.statement_id) !== statementId) return;
    finish({
      status: "failed",
      finished_at: new Date().toISOString(),
      error_message: d?.message || d?.error || "Statement parsing failed.",
    });
  }

  function onProgress(ev: Event) {
    const d = (ev as CustomEvent<{ source_file?: string; current_page?: number; total_pages?: number }>)
      .detail;
    if (!d?.source_file || d.source_file !== fileName) return;
    const current = useProcessingQueue.getState().rows[rowId];
    if (!current || TERMINAL_STATUSES.has(current.status)) return;
    useProcessingQueue.getState().applyJobUpdate({
      ...(current as Job),
      status: "running",
      pages_done: d.current_page ?? current.pages_done ?? null,
      pages_total: d.total_pages ?? current.pages_total ?? null,
    });
  }

  window.addEventListener("statement_parsed", onParsed);
  window.addEventListener("statement_failed", onFailed);
  window.addEventListener("statement_parse_progress", onProgress);

  poll = setInterval(() => {
    void (async () => {
      try {
        const row = await api.get<StatementRow>(`/api/statements/${statementId}`);
        if (row?.status === "imported") {
          finish({
            status: "done",
            finished_at: new Date().toISOString(),
            result: {
              statement_id: statementId,
              summary: {
                rows: row.row_count ?? null,
                institution: row.institution ?? null,
                source_file: fileName,
              },
            },
          });
        } else if (row?.status === "failed") {
          finish({
            status: "failed",
            finished_at: new Date().toISOString(),
            error_message: row.status_message || "Statement parsing failed.",
          });
        }
      } catch {
        // Transient — next tick tries again. Never a reason to fail the row.
      }
    })();
  }, 3_000);
}

// ========================= Server sync ================================

const ACTIVE_POLL_MS = 3_000;
/** Idle heartbeat. Something another tab started has to become visible here
 *  eventually even if SSE never reconnects. */
const IDLE_POLL_MS = 20_000;

let syncTimer: ReturnType<typeof setInterval> | null = null;
let syncTimerInterval = 0;
let watchers = 0;
let inFlightFetch: Promise<void> | null = null;

export async function refreshJobs(): Promise<void> {
  if (inFlightFetch) return inFlightFetch;
  const run = (async () => {
    try {
      const data = await api.get<JobsResponse>("/api/jobs?limit=500");
      useProcessingQueue.getState().applyJobs(data ?? { jobs: [] });
      if (!useProcessingQueue.getState().jobsApiAvailable) {
        useProcessingQueue.setState({ jobsApiAvailable: true });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Backend predates the queue. Run on local rows; say nothing alarming.
        useProcessingQueue.setState({
          jobsApiAvailable: false,
          syncError: null,
          syncFailedAt: null,
          lastSyncAt: Date.now(),
        });
      } else if (err instanceof ApiError && err.status === 401) {
        // Signed out or token rotating — the next tick retries. Still a failed
        // read, so the counts on screen are a memory until one succeeds.
        useProcessingQueue.setState({ syncFailedAt: Date.now() });
      } else {
        useProcessingQueue.setState({
          syncError: describeError(err),
          syncFailedAt: Date.now(),
        });
      }
    } finally {
      inFlightFetch = null;
      ensureSyncCadence();
    }
  })();
  inFlightFetch = run;
  return run;
}

/** Re-pick the poll interval for whatever is going on right now. */
function ensureSyncCadence() {
  if (watchers <= 0) {
    if (syncTimer) {
      clearInterval(syncTimer);
      syncTimer = null;
      syncTimerInterval = 0;
    }
    return;
  }
  const s = useProcessingQueue.getState();
  const busy = selectInFlight(s) || activeUploads > 0 || uploadQueue.length > 0;
  const want = busy ? ACTIVE_POLL_MS : IDLE_POLL_MS;
  if (syncTimer && syncTimerInterval === want) return;
  if (syncTimer) clearInterval(syncTimer);
  syncTimerInterval = want;
  syncTimer = setInterval(() => {
    if (!useProcessingQueue.getState().jobsApiAvailable) return;
    void refreshJobs();
  }, want);
}

/**
 * Start syncing. Called from the app shell so every authenticated page loads
 * the queue once and keeps it current. Returns the matching stop function.
 */
export function startProcessingQueueSync(): () => void {
  watchers++;
  if (watchers === 1) {
    void refreshJobs();
  }
  ensureSyncCadence();
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    watchers--;
    ensureSyncCadence();
  };
}

function invalidateListQueries() {
  for (const key of [
    ["transactions"],
    ["transactions-date-range"],
    ["receipts"],
    ["source-files"],
    ["statements"],
    ["dashboard"],
    ["analytics"],
    ["available-matches"],
    ["link-candidates"],
    ["transaction"],
    ["transaction-proofs"],
    ["pendingReview"],
  ]) {
    queryClient.invalidateQueries({ queryKey: key });
  }
}

// ====================== SSE + lifecycle wiring ========================
// `use-sse.ts` re-dispatches named stream events as window CustomEvents. The
// store listens here rather than in a component so the queue keeps tracking
// while the user is on some other page.

if (typeof window !== "undefined") {
  window.addEventListener("job_updated", (ev) => {
    const detail = (ev as CustomEvent<Job & { type?: string }>).detail;
    if (!detail || !detail.id) return;
    useProcessingQueue.getState().applyJobUpdate({ ...detail, id: String(detail.id) });
    // Live events prove the stream is up; a 404ing jobs API does not. They also
    // prove the server is answering, which clears the "last known" note.
    useProcessingQueue.setState({
      connection: "live",
      jobsApiAvailable: true,
      syncFailedAt: null,
      lastSyncAt: Date.now(),
    });
    ensureSyncCadence();
  });

  window.addEventListener("jobs_summary", (ev) => {
    const detail = (
      ev as CustomEvent<
        Partial<JobCounts> & {
          worker?: Partial<WorkerInfo>;
          batch?: BatchProgress | null;
          type?: string;
        }
      >
    ).detail;
    if (!detail) return;
    const { worker, batch, type: _type, ...counts } = detail;
    void _type;
    useProcessingQueue.getState().applySummary(counts, worker, batch);
    useProcessingQueue.setState({ connection: "live" });
    ensureSyncCadence();
  });

  window.addEventListener("sse_open", () => {
    useProcessingQueue.setState({ connection: "live" });
    // A reconnect may have missed events while it was down.
    if (useProcessingQueue.getState().jobsApiAvailable) void refreshJobs();
  });

  window.addEventListener("sse_closed", () => {
    useProcessingQueue.setState({ connection: "polling" });
    ensureSyncCadence();
  });

  // Coming back to a backgrounded tab: browsers throttle timers there, so the
  // first thing to do is re-read the truth rather than show a stale strip.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (watchers > 0 && useProcessingQueue.getState().jobsApiAvailable) {
      void refreshJobs();
    }
  });

  // Only warn about bytes still going up. A queued server-side job finishes
  // whether or not this tab stays open, so it is not a reason to nag.
  window.addEventListener("beforeunload", (e) => {
    const uploading = Object.values(useProcessingQueue.getState().rows).some(
      (r) => r.status === "uploading",
    );
    if (uploading || uploadQueue.length > 0) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}
