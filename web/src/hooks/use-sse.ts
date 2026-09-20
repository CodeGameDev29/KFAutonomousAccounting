import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/auth-store";
import { API_URL } from "@/lib/constants";

type SSEEvent =
  | "receipts_updated"
  | "transactions_updated"
  | "reconciliation_complete"
  | "categorization_complete"
  | "extraction_complete"
  | "onboarding_updated"
  | "sync_progress"
  | "review_updated"
  | "connections_health_changed"
  | "proofs_updated"
  | "scan_progress"
  | "scan_complete"
  | "scan_failed"
  | "statement_parse_started"
  | "statement_parse_progress"
  | "statement_parse_complete"
  | "statement_parse_error"
  | "statement_parsed"
  | "statement_failed"
  | "job_updated"
  | "jobs_summary";

const EVENT_TO_QUERY_KEYS: Record<SSEEvent, string[][]> = {
  receipts_updated: [["receipts"]],
  transactions_updated: [["transactions"], ["dashboard"], ["analytics"], ["source-files"]],
  reconciliation_complete: [["reconciliation"], ["transactions"], ["dashboard"], ["analytics"]],
  categorization_complete: [["transactions"], ["dashboard"], ["analytics"], ["recategorize-progress"]],
  extraction_complete: [["receipts"], ["transactions"]],
  onboarding_updated: [["onboarding"]],
  sync_progress: [["dashboard"]],
  review_updated: [["review"], ["transactions"], ["dashboard"]],
  connections_health_changed: [["connections-health"]],
  proofs_updated: [["transaction-proofs"], ["transactions"], ["receipts"], ["dashboard"], ["review-match-counts"]],
  scan_progress: [["scan-status"]],
  scan_complete: [["scan-status"], ["scan-history"], ["receipts"], ["connections", "health"]],
  scan_failed: [["scan-status"], ["scan-history"]],
  statement_parse_started: [["transactions"], ["statements"], ["pendingReview"]],
  statement_parse_progress: [["transactions"], ["statements"], ["pendingReview"]],
  statement_parse_complete: [["transactions"], ["statements"], ["pendingReview"]],
  statement_parse_error: [["transactions"], ["statements"], ["pendingReview"]],
  // Terminal states of a background statement parse (the 202 upload flow).
  statement_parsed: [["transactions"], ["transactions-date-range"], ["statements"], ["source-files"], ["pendingReview"], ["dashboard"], ["analytics"]],
  statement_failed: [["statements"]],
  // Deliberately no query keys: a large import pushes thousands of these, and
  // invalidating the transaction/receipt caches on every progress tick would
  // re-fetch the world once per tick. The processing-queue store
  // invalidates exactly once per job, when the job reaches a terminal state.
  job_updated: [],
  jobs_summary: [],
};

// Events that are re-dispatched as window CustomEvents, because a listener
// somewhere needs the payload and not just a cache invalidation.
const DOM_FORWARDED_EVENTS = new Set<string>([
  "scan_progress",
  "scan_complete",
  "scan_failed",
  "statement_parse_started",
  "statement_parse_progress",
  "statement_parse_complete",
  "statement_parse_error",
  "statement_parsed",
  "statement_failed",
  // The processing-queue store listens for these on window rather than
  // importing the hook, so it keeps tracking a run while the user is on any
  // page — or on no page the queue UI is mounted on at all.
  "job_updated",
  "jobs_summary",
]);

/**
 * SSE hook that connects to the server event stream and automatically
 * invalidates relevant TanStack Query caches when events arrive.
 */
export function useSSE() {
  const queryClient = useQueryClient();
  const eventSourceRef = useRef<EventSource | null>(null);
  const isAuthenticated = useAuthStore((s) => !!s.user);
  const initialized = useAuthStore((s) => s.initialized);
  const onboardingChecked = useAuthStore((s) => s.onboardingChecked);

  // SSE connect uses a single-use ticket fetched via POST /api/events/ticket.
  // The raw JWT is never placed in the URL, so it can't end up in browser
  // history, Referer headers, or reverse-proxy access logs.
  const sseDisabled = false;

  useEffect(() => {
    if (sseDisabled) return;
    // Wait for auth to fully initialize AND onboarding check to complete before
    // attempting SSE connection. onboardingChecked being true means the token has
    // been validated by at least one successful API call (checkOnboardingStatus),
    // so the token is confirmed usable. This prevents 401 errors from stale tokens.
    if (!isAuthenticated || !initialized || !onboardingChecked) {
      // Close any existing connection if user logs out or auth not yet settled
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      return;
    }

    // Retry state lives outside connect() so it persists across reconnections.
    //
    // There is no retry ceiling, deliberately. A bounded one burns its whole
    // budget during an ordinary API restart, and the stream then stays dead
    // until the page is reloaded — which, in the middle of a long import, is
    // exactly when nobody is watching the tab. Anything that stops the stream
    // here is transient by definition (the server restarting, a proxy blip, a
    // sleeping machine), so it keeps trying: 5s, 10s, 20s, 40s, then every 60s,
    // until it answers or the component unmounts.
    let retryCount = 0;
    const MIN_RETRY_MS = 5_000;
    const MAX_RETRY_MS = 60_000;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const scheduleReconnect = () => {
      if (disposed || retryTimer) return;
      retryCount++;
      const delay = Math.min(
        MAX_RETRY_MS,
        MIN_RETRY_MS * Math.pow(2, Math.max(0, retryCount - 1)),
      );
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void connect();
      }, delay);
    };

    /** Try again right now — the tab came back, or the network did. */
    const reconnectNow = () => {
      if (disposed) return;
      if (eventSourceRef.current) return; // already connected or connecting
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      retryCount = 0;
      void connect();
    };

    const fetchTicket = async (): Promise<string | null> => {
      const accessToken = await useAuthStore.getState().getAccessToken();
      if (!accessToken) return null;
      try {
        const res = await fetch(`${API_URL}/api/events/ticket`, {
          method: "POST",
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (!res.ok) return null;
        const data = (await res.json()) as { ticket?: string };
        return data.ticket ?? null;
      } catch {
        return null;
      }
    };

    const connect = async () => {
      if (disposed) return;
      // Mint a fresh single-use ticket for every (re)connect. Tickets expire
      // in 60s and are consumed at stream open, so one per connect is correct.
      const ticket = await fetchTicket();
      if (!ticket) {
        // No ticket, no stream — and a 502 from a restarting API is exactly as
        // temporary as a dropped stream, so this schedules a retry rather than
        // giving up. Giving up would strand the page: one ticket POST that
        // fails during a restart would cost live updates until a reload.
        window.dispatchEvent(new Event("sse_closed"));
        scheduleReconnect();
        return;
      }

      const url = `${API_URL}/api/events/stream?ticket=${encodeURIComponent(ticket)}`;
      const eventSource = new EventSource(url);
      eventSourceRef.current = eventSource;

      // Whether the stream is up is itself state the UI shows ("live updates
      // paused, refreshing every 3 s"), so it is broadcast like any other event.
      eventSource.onopen = () => {
        // A live stream again: reset the backoff and tell the UI, so the
        // "live updates paused" note and the muted counts clear on their own,
        // with no reload.
        retryCount = 0;
        window.dispatchEvent(new Event("sse_open"));
      };

      // One handler for every event, whatever carried it here. `type` is the
      // event name; `raw` is the JSON payload the server put in `data:`.
      const handleEvent = (type: string, raw: string) => {
        // Reset retry count on successful message receipt (connection is healthy)
        retryCount = 0;
        let payload: Record<string, unknown>;
        try {
          payload = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
        } catch {
          return; // Ignore malformed events (e.g. keepalive pings)
        }
        const detail = { ...payload, type };

        // Gmail forwarding confirmation: dispatch DOM event for walkthrough
        if (type === "gmail_forwarding_confirmation") {
          window.dispatchEvent(
            new CustomEvent("gmail_forwarding_confirmation", {
              detail: { code: payload.code },
            })
          );
          return;
        }

        // Scan, statement-parse and statement-terminal events are also
        // dispatched as window events, so hooks and the upload queue can react
        // to the payload itself rather than just re-fetching.
        if (DOM_FORWARDED_EVENTS.has(type)) {
          window.dispatchEvent(new CustomEvent(type, { detail }));
        }

        const queryKeys = EVENT_TO_QUERY_KEYS[type as SSEEvent];
        if (queryKeys) {
          queryKeys.forEach((key) => {
            queryClient.invalidateQueries({ queryKey: key });
          });
        }
      };

      // The server sends every update as a *named* SSE event
      // (`event: statement_parsed\ndata: {...}`). A named event NEVER reaches
      // `onmessage` — per the EventSource spec that only fires for events with
      // no name — so each type needs its own listener or the payload is
      // silently dropped on the floor.
      (Object.keys(EVENT_TO_QUERY_KEYS) as SSEEvent[])
        .concat(["gmail_forwarding_confirmation" as SSEEvent])
        .forEach((name) => {
          eventSource.addEventListener(name, (e) =>
            handleEvent(name, (e as MessageEvent).data as string)
          );
        });

      // Unnamed events (and any future sender that puts the type in the body).
      eventSource.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as { type?: string };
          if (payload && typeof payload.type === "string") {
            handleEvent(payload.type, event.data);
          }
        } catch {
          // Ignore malformed events (e.g. keepalive pings)
        }
      };

      eventSource.onerror = () => {
        eventSource.close();
        eventSourceRef.current = null;
        window.dispatchEvent(new Event("sse_closed"));
        scheduleReconnect();
      };
    };

    void connect();

    // Two things that mean "try again now" rather than "wait out the backoff":
    // the tab came back to the foreground (timers are throttled in background
    // tabs, so the next attempt could be minutes away), and the machine got its
    // network back. Both are the moments a user is most likely to be looking.
    const onVisible = () => {
      if (document.visibilityState === "visible") reconnectNow();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", reconnectNow);

    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", reconnectNow);
      // Clear pending retry timer to prevent connect() firing after unmount
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
    };
  }, [isAuthenticated, initialized, onboardingChecked, queryClient]);
}
