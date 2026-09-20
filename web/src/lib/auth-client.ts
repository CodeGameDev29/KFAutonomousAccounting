/**
 * The browser half of the local identity service.
 *
 * It talks to `/api/auth/*` on the same origin (see `server/local_auth.py`) and
 * presents a `{ data, error }` result rather than throwing — the shape
 * `stores/auth-store.ts` is written against, so that store deals in sessions
 * and never in transport.
 *
 * Session persistence is localStorage under one key. Same-origin API means no
 * CORS preflight and no third-party cookie concerns.
 *
 * Refreshing is coordinated twice over. Without it, several tabs open on a long
 * run sign the user out mid-run: two tabs race to spend the same refresh token
 * and the loser is handed a 401.
 *
 *   * **once per tab** — `refreshSession()` collapses concurrent callers onto
 *     one in-flight promise (`auth-store.ts` also does this for its own callers,
 *     but this entry point is reachable from elsewhere and has to be safe alone);
 *   * **once per browser** — one tab wins a `localStorage` lock, POSTs, and
 *     broadcasts the resulting session over a `BroadcastChannel`; the others
 *     adopt it. The server also holds a rotated token valid for 60s
 *     (`server/local_auth.py::rotate_refresh_token`), so a tab that gives up
 *     waiting and refreshes anyway still gets the same live pair.
 *
 * And only a 401/403 from `/api/auth/refresh` ever clears the session. A 502,
 * a 503 or a dropped socket means the API is restarting or briefly unreachable,
 * and must leave the stored session alone for the caller's backoff to retry.
 */

const STORAGE_KEY = "aa.session";

/** Cross-tab refresh coordination. */
const REFRESH_CHANNEL_NAME = "aa.auth";
const REFRESH_LOCK_KEY = "aa.auth.refresh-lock";
/** How long another tab's claim on the refresh is trusted before it is taken over. */
const REFRESH_LOCK_TTL_MS = 10_000;
/** How long a follower waits for the leader's broadcast before refreshing itself. */
const PEER_RESULT_TIMEOUT_MS = 4_000;

export interface AuthUser {
  id: string;
  email: string | null;
  aud: string;
  role: string;
  email_confirmed_at: string | null;
  confirmed_at: string | null;
  last_sign_in_at: string | null;
  created_at: string | null;
  app_metadata: { provider?: string; providers?: string[] };
  user_metadata: { email_verified?: boolean } & Record<string, unknown>;
  is_active?: boolean;
}

export interface Session {
  access_token: string;
  token_type: string;
  /** Seconds the access token is valid for, from issuance. */
  expires_in: number;
  /** Absolute expiry, epoch seconds. Survives a page reload; expires_in does not. */
  expires_at: number;
  refresh_token: string;
  user: AuthUser;
}

export interface AuthError {
  message: string;
  status?: number;
}

export type AuthChangeEvent =
  | "INITIAL_SESSION"
  | "SIGNED_IN"
  | "SIGNED_OUT"
  | "TOKEN_REFRESHED"
  | "USER_UPDATED";

type Listener = (event: AuthChangeEvent, session: Session | null) => void;

// -- Session storage --------------------------------------------------

function readStoredSession(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    if (!parsed?.access_token || !parsed?.refresh_token) return null;
    return parsed;
  } catch {
    // Corrupt or unavailable storage (private mode, cleared site data) is not
    // an error condition — it just means "not signed in".
    return null;
  }
}

function writeStoredSession(session: Session | null): void {
  try {
    if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable — the in-memory session still works for this tab */
  }
}

let currentSession: Session | null = readStoredSession();
const listeners = new Set<Listener>();

function emit(event: AuthChangeEvent, session: Session | null): void {
  for (const fn of listeners) {
    try {
      fn(event, session);
    } catch {
      /* a throwing subscriber must not stop the others */
    }
  }
}

function setSession(session: Session | null, event: AuthChangeEvent): void {
  currentSession = session;
  writeStoredSession(session);
  emit(event, session);
}

// -- Transport --------------------------------------------------------

async function post<T>(
  path: string,
  body?: unknown,
  opts: { auth?: boolean } = {},
): Promise<{ data: T | null; error: AuthError | null }> {
  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (opts.auth && currentSession) {
      headers.Authorization = `Bearer ${currentSession.access_token}`;
    }
    const resp = await fetch(path, {
      method: "POST",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await resp.text();
    const json = text ? JSON.parse(text) : {};
    if (!resp.ok) {
      return {
        data: null,
        error: {
          message: json.error || json.detail || `Request failed (${resp.status})`,
          status: resp.status,
        },
      };
    }
    return { data: json as T, error: null };
  } catch (e) {
    return { data: null, error: { message: (e as Error).message || "Network error" } };
  }
}

// -- Refreshing: once per tab, once per browser ------------------------

type RefreshResult = { data: { session: Session | null }; error: AuthError | null };

type RefreshMessage =
  | { type: "refresh_result"; from: string; session: Session }
  | { type: "refresh_failed"; from: string; hard: boolean };

/** What a follower learned from the tab that actually did the refresh. */
type PeerOutcome = "adopted" | "hard_failure" | "soft_failure";

const TAB_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** The single in-flight refresh for this tab. */
let inFlightRefresh: Promise<RefreshResult> | null = null;

let channel: BroadcastChannel | null = null;
let channelChecked = false;
const peerWaiters = new Set<(outcome: PeerOutcome) => void>();

function resolvePeerWaiters(outcome: PeerOutcome): void {
  const waiting = [...peerWaiters];
  peerWaiters.clear();
  for (const resolve of waiting) resolve(outcome);
}

function handlePeerMessage(msg: RefreshMessage | null | undefined): void {
  if (!msg || msg.from === TAB_ID) return;

  if (msg.type === "refresh_result") {
    const incoming = msg.session;
    const usable = !!incoming?.access_token && !!incoming?.refresh_token;
    // Adopted even by a tab that was not waiting: that is what stops a
    // background tab from sitting on a refresh token its sibling has spent.
    // Only a tab that still believes it is signed in adopts, so a sign-out
    // racing a refresh cannot resurrect the session.
    if (usable && currentSession && (incoming.expires_at ?? 0) >= (currentSession.expires_at ?? 0)) {
      setSession(incoming, "TOKEN_REFRESHED");
      resolvePeerWaiters("adopted");
      return;
    }
    resolvePeerWaiters(usable && currentSession ? "adopted" : "soft_failure");
    return;
  }

  // The leader failed. Only its hard failure (a 401/403 — the token really is
  // dead, and it is the same token every tab holds) ends the session here too.
  if (msg.hard) setSession(null, "SIGNED_OUT");
  resolvePeerWaiters(msg.hard ? "hard_failure" : "soft_failure");
}

function getChannel(): BroadcastChannel | null {
  if (channelChecked) return channel;
  channelChecked = true;
  if (typeof BroadcastChannel === "undefined") return null;
  try {
    channel = new BroadcastChannel(REFRESH_CHANNEL_NAME);
    channel.onmessage = (ev: MessageEvent<RefreshMessage>) => handlePeerMessage(ev.data);
  } catch {
    channel = null;
  }
  return channel;
}

function publish(bus: BroadcastChannel, msg: RefreshMessage): void {
  try {
    bus.postMessage(msg);
  } catch {
    /* a closed channel is not a refresh failure */
  }
}

/**
 * Claim the right to be the one tab that POSTs. Not a real mutex — two tabs
 * writing in the same instant both re-read, the last write wins, and the other
 * becomes a follower. Stale claims (a tab that was closed mid-refresh) expire.
 */
function claimRefreshLock(): boolean {
  try {
    const raw = localStorage.getItem(REFRESH_LOCK_KEY);
    if (raw) {
      const held = JSON.parse(raw) as { id?: string; at?: number } | null;
      const age = Date.now() - (held?.at ?? 0);
      if (held?.id && held.id !== TAB_ID && age >= 0 && age < REFRESH_LOCK_TTL_MS) return false;
    }
    localStorage.setItem(REFRESH_LOCK_KEY, JSON.stringify({ id: TAB_ID, at: Date.now() }));
    const confirmed = JSON.parse(localStorage.getItem(REFRESH_LOCK_KEY) ?? "null") as
      | { id?: string }
      | null;
    return confirmed?.id === TAB_ID;
  } catch {
    // No storage to coordinate through — behave like a single tab.
    return true;
  }
}

function releaseRefreshLock(): void {
  try {
    const raw = localStorage.getItem(REFRESH_LOCK_KEY);
    if (!raw) return;
    const held = JSON.parse(raw) as { id?: string } | null;
    if (held?.id === TAB_ID) localStorage.removeItem(REFRESH_LOCK_KEY);
  } catch {
    /* nothing to release */
  }
}

function waitForPeerRefresh(timeoutMs: number): Promise<PeerOutcome | "timeout"> {
  return new Promise((resolve) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onOutcome = (outcome: PeerOutcome) => {
      clearTimeout(timer);
      resolve(outcome);
    };
    timer = setTimeout(() => {
      peerWaiters.delete(onOutcome);
      resolve("timeout");
    }, timeoutMs);
    peerWaiters.add(onOutcome);
  });
}

/** A 401/403 from the refresh endpoint is the only thing that ends a session. */
function isHardAuthFailure(error: AuthError | null): boolean {
  return error?.status === 401 || error?.status === 403;
}

async function performRefresh(): Promise<RefreshResult> {
  const refresh_token = currentSession?.refresh_token;
  if (!refresh_token) {
    return { data: { session: null }, error: { message: "No refresh token" } };
  }
  const { data, error } = await post<Session>("/api/auth/refresh", { refresh_token });
  if (!currentSession) {
    // Signed out while this was in flight. Adopting the answer now would
    // resurrect a session the user just ended.
    return { data: { session: null }, error: { message: "Signed out" } };
  }
  if (error || !data) {
    // Deliberately narrow: a network error or a 5xx (the uvicorn process
    // restarting) leaves the stored session exactly where it is.
    if (isHardAuthFailure(error)) setSession(null, "SIGNED_OUT");
    return { data: { session: null }, error: error ?? { message: "Refresh failed" } };
  }
  setSession(data, "TOKEN_REFRESHED");
  return { data: { session: data }, error: null };
}

async function coordinatedRefresh(): Promise<RefreshResult> {
  if (!currentSession?.refresh_token) {
    return { data: { session: null }, error: { message: "No refresh token" } };
  }

  const bus = getChannel();
  if (!bus) return performRefresh();

  if (!claimRefreshLock()) {
    const outcome = await waitForPeerRefresh(PEER_RESULT_TIMEOUT_MS);
    if (outcome === "adopted") return { data: { session: currentSession }, error: null };
    if (outcome === "hard_failure") {
      return { data: { session: null }, error: { message: "Session expired", status: 401 } };
    }
    // Timed out, or the leader failed softly. Refresh here rather than hang:
    // the server's 60s grace means presenting the same token again returns the
    // same successor instead of a 401.
    return performRefresh();
  }

  try {
    const result = await performRefresh();
    if (result.data.session) {
      publish(bus, { type: "refresh_result", from: TAB_ID, session: result.data.session });
    } else {
      publish(bus, {
        type: "refresh_failed",
        from: TAB_ID,
        hard: isHardAuthFailure(result.error),
      });
    }
    return result;
  } finally {
    releaseRefreshLock();
  }
}

// Opened at load, not on first refresh: a tab that never refreshes still has to
// hear a sibling's new session, or it keeps a token that is about to be spent.
getChannel();

// -- Public client ----------------------------------------------------

export const authClient = {
  auth: {
    async getSession(): Promise<{ data: { session: Session | null }; error: AuthError | null }> {
      return { data: { session: currentSession }, error: null };
    },

    /**
     * Subscribe to auth changes. Fires INITIAL_SESSION synchronously on
     * subscribe, matching the ordering `auth-store.ts` was written against.
     */
    onAuthStateChange(callback: Listener): {
      data: { subscription: { unsubscribe: () => void } };
    } {
      listeners.add(callback);
      try {
        callback("INITIAL_SESSION", currentSession);
      } catch {
        /* ignore */
      }
      return {
        data: {
          subscription: {
            unsubscribe: () => {
              listeners.delete(callback);
            },
          },
        },
      };
    },

    async signInWithPassword(creds: { email: string; password: string }): Promise<{
      data: { session: Session | null; user: AuthUser | null };
      error: AuthError | null;
    }> {
      const { data, error } = await post<Session>("/api/auth/token", creds);
      if (error || !data) {
        return { data: { session: null, user: null }, error: error ?? { message: "Sign-in failed" } };
      }
      setSession(data, "SIGNED_IN");
      return { data: { session: data, user: data.user }, error: null };
    },

    /**
     * Exchange the refresh token for a new session.
     *
     * Safe to call from anywhere, as often as you like: concurrent callers in
     * this tab share one in-flight promise, and across tabs exactly one POST is
     * made and its result broadcast to the rest. A failure that is not a
     * 401/403 leaves the session in place for the caller to retry — see the
     * module docstring.
     */
    async refreshSession(): Promise<{
      data: { session: Session | null };
      error: AuthError | null;
    }> {
      if (inFlightRefresh) return inFlightRefresh;
      inFlightRefresh = coordinatedRefresh().finally(() => {
        inFlightRefresh = null;
      });
      return inFlightRefresh;
    },

    async signOut(): Promise<{ error: AuthError | null }> {
      // Best effort: even if revocation fails (server down, token already
      // expired) the local session must still be cleared.
      if (currentSession) {
        await post("/api/auth/logout", { refresh_token: currentSession.refresh_token }, { auth: true });
      }
      setSession(null, "SIGNED_OUT");
      return { error: null };
    },

    /** Read the user fresh from the server, so email confirmation is noticed. */
    async getUser(): Promise<{ data: { user: AuthUser | null }; error: AuthError | null }> {
      if (!currentSession) return { data: { user: null }, error: { message: "Not signed in" } };
      try {
        const resp = await fetch("/api/auth/user", {
          headers: { Authorization: `Bearer ${currentSession.access_token}` },
        });
        if (!resp.ok) {
          return { data: { user: null }, error: { message: "Failed to load user", status: resp.status } };
        }
        const json = (await resp.json()) as { user: AuthUser };
        // Keep the cached session's user in step so `emailVerified` derived
        // from it does not go stale after confirmation.
        if (currentSession) {
          currentSession = { ...currentSession, user: json.user };
          writeStoredSession(currentSession);
        }
        return { data: { user: json.user }, error: null };
      } catch (e) {
        return { data: { user: null }, error: { message: (e as Error).message } };
      }
    },

    /** Re-send a confirmation link. */
    async resend(args: { type?: string; email: string }): Promise<{ error: AuthError | null }> {
      const { error } = await post("/api/auth/resend", { email: args.email });
      return { error };
    },

    /** Set a new password for the signed-in user, or via a recovery token. */
    async updateUser(attrs: { password?: string; token?: string }): Promise<{
      error: AuthError | null;
    }> {
      if (!attrs.password) return { error: { message: "No password supplied" } };
      const { error } = await post(
        "/api/auth/update-password",
        { password: attrs.password, token: attrs.token ?? null },
        { auth: true },
      );
      if (!error) emit("USER_UPDATED", currentSession);
      return { error };
    },

    /** Confirm an email address with the token from the confirmation link. */
    async confirmEmail(token: string): Promise<{
      data: { session: Session | null };
      error: AuthError | null;
    }> {
      const { data, error } = await post<Session>("/api/auth/confirm", { token });
      if (error || !data) return { data: { session: null }, error: error ?? { message: "Confirm failed" } };
      setSession(data, "SIGNED_IN");
      return { data: { session: data }, error: null };
    },

    /**
     * Adopt a session the server minted outside the sign-in flow — currently
     * only auto-confirmed signup, which returns a live session so the browser
     * does not have to immediately post the same credentials back.
     */
    adoptSession(session: Session): void {
      setSession(session, "SIGNED_IN");
    },

    /** Whether the server has a Google OAuth client configured. */
    async googleEnabled(): Promise<boolean> {
      try {
        const r = await fetch("/api/auth/google/status");
        if (!r.ok) return false;
        return !!(await r.json()).enabled;
      } catch {
        return false;
      }
    },

    /**
     * Hand the browser to Google. A full navigation, not fetch(): the consent
     * screen is a page the user has to see and interact with.
     */
    startGoogleSignIn(): void {
      window.location.href = "/api/auth/google/start";
    },

    /**
     * Finish Google sign-in by trading the single-use ticket for a session.
     *
     * The ticket rather than the tokens themselves is what came back in the
     * URL, so nothing reusable is left in browser history once this resolves.
     */
    async completeGoogleSignIn(ticket: string): Promise<{
      data: { session: Session | null };
      error: AuthError | null;
    }> {
      const { data, error } = await post<Session>("/api/auth/google/exchange", { ticket });
      if (error || !data) {
        return { data: { session: null }, error: error ?? { message: "Sign-in failed" } };
      }
      setSession(data, "SIGNED_IN");
      return { data: { session: data }, error: null };
    },
  },
};

export default authClient;
