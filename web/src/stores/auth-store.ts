import { create } from "zustand";
import type { AuthUser as User, Session } from "@/lib/auth-client";
import { authClient } from "@/lib/auth-client";

interface AuthState {
  user: User | null;
  session: Session | null;
  loading: boolean;
  initialized: boolean;
  onboarded: boolean;
  onboardingChecked: boolean;
  /** Whether the current user's email has been verified */
  emailVerified: boolean;
  /** Non-zero epoch-ms timestamp while a refresh backoff is in effect */
  rateLimitedUntil: number;
  initialize: () => () => void;
  checkOnboardingStatus: () => Promise<void>;
  checkEmailVerification: () => Promise<void>;
  signInWithEmail: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<{ confirmUrl?: string }>;
  signOut: () => Promise<void>;
  getAccessToken: () => Promise<string | null>;
  forceRefreshToken: () => Promise<string | null>;
  setOnboarded: (value: boolean) => void;
}

// Deduplication guard: when multiple callers request a token refresh
// simultaneously (e.g., 3 TanStack Query calls on Dashboard mount), only ONE
// actual refresh is made, and every caller gets the same answer.
//
// Two more layers sit under this one, so a lost race can no longer end a
// session: `authClient.auth.refreshSession()` collapses its own concurrent
// callers and elects one tab per browser to do the POST (see auth-client.ts),
// and the server answers a token it already rotated with the same successor for
// 60 seconds (server/local_auth.py). This guard stays because it is the cheapest
// of the three and keeps the store's backoff bookkeeping single-threaded.
let _refreshPromise: Promise<string | null> | null = null;

// Proactive refresh timer handle. Refreshing is scheduled here rather than
// left to the client library, so every refresh goes through
// deduplicatedRefresh() and cannot race.
let _refreshTimer: ReturnType<typeof setTimeout> | null = null;

// Token expiry timestamp (epoch seconds).
let _localExpiresAt: number | null = null;

// Backoff: when a refresh fails, block further attempts for this many ms
// rather than hammering the auth endpoint.
let _refreshBackoffUntil = 0;

// Recovery timer: schedules a retry after rate-limit backoff expires.
let _recoveryTimer: ReturnType<typeof setTimeout> | null = null;

const RATE_LIMIT_COOLDOWN_MS = 60_000; // 60 seconds

function updateLocalExpiry(session: Session | null): void {
  if (!session) {
    _localExpiresAt = null;
    return;
  }
  // Prefer expires_at (absolute server timestamp) over expires_in (original TTL).
  // On cold page load, expires_in still holds the original TTL from issuance,
  // which masks actual expiry. expires_at is the true expiry timestamp.
  // Only fall back to expires_in for freshly-issued sessions (where both are accurate).
  if (session.expires_at) {
    _localExpiresAt = session.expires_at;
  } else {
    const ttl = session.expires_in || 3600;
    _localExpiresAt = Math.floor(Date.now() / 1000) + ttl;
  }
}

/**
 * Schedule a proactive token refresh ~60 seconds before expiry.
 * Uses deduplicatedRefresh() so concurrent calls are safe.
 */
function scheduleProactiveRefresh(
  set: (partial: Partial<AuthState>) => void,
): void {
  if (_refreshTimer) {
    clearTimeout(_refreshTimer);
    _refreshTimer = null;
  }
  if (!_localExpiresAt) return;

  const now = Math.floor(Date.now() / 1000);
  // Refresh 60 seconds before expiry, minimum 10 seconds from now
  const refreshIn = Math.max((_localExpiresAt - now - 60) * 1000, 10_000);
  _refreshTimer = setTimeout(() => {
    _refreshTimer = null;
    deduplicatedRefresh(set).then((token) => {
      if (token) {
        // Re-schedule after successful refresh
        scheduleProactiveRefresh(set);
      }
    });
  }, refreshIn);
}

function enterRateLimitBackoff(
  set: (partial: Partial<AuthState>) => void,
): void {
  const until = Date.now() + RATE_LIMIT_COOLDOWN_MS;
  _refreshBackoffUntil = until;
  set({ rateLimitedUntil: until });

  // Schedule a recovery refresh after cooldown
  if (_recoveryTimer) clearTimeout(_recoveryTimer);
  _recoveryTimer = setTimeout(() => {
    _recoveryTimer = null;
    deduplicatedRefresh(set).then((token) => {
      if (token) scheduleProactiveRefresh(set);
    });
  }, RATE_LIMIT_COOLDOWN_MS + 1_000); // +1s margin
}

function deduplicatedRefresh(
  set: (partial: Partial<AuthState>) => void,
): Promise<string | null> {
  // A recent failure (e.g. a 429 rate limit) blocks a retry until it expires
  if (Date.now() < _refreshBackoffUntil) {
    return Promise.resolve(null);
  }
  if (!_refreshPromise) {
    _refreshPromise = authClient.auth
      .refreshSession()
      .then(({ data, error }) => {
        if (error || !data.session) {
          enterRateLimitBackoff(set);
          return null;
        }
        _refreshBackoffUntil = 0; // Clear backoff on success
        set({ session: data.session, user: data.session.user, rateLimitedUntil: 0 });
        updateLocalExpiry(data.session);
        return data.session.access_token;
      })
      .catch(() => {
        enterRateLimitBackoff(set);
        return null;
      })
      .finally(() => {
        _refreshPromise = null;
      });
  }
  return _refreshPromise;
}

/** Determine emailVerified from a user record. */
function isEmailVerified(user: User | null): boolean {
  if (!user) return true; // safe default when no user
  return !!user.email_confirmed_at;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  session: null,
  loading: true,
  initialized: false,
  onboarded: false,
  onboardingChecked: false,
  emailVerified: true, // safe default — already-verified users pass through
  rateLimitedUntil: 0,

  initialize: () => {
    // Deduplication flag to prevent checkOnboardingStatus() from being called
    // by both the getSession callback and the onAuthStateChange listener.
    let onboardingCheckTriggered = false;

    // Get the initial session
    authClient.auth.getSession().then(async ({ data }) => {
      let session = data.session;

      // On cold page load, the stored session's access token may be expired.
      // getSession() reads localStorage and does NOT refresh it.
      // Check expires_at and proactively refresh before any API call.
      // IMPORTANT: Use deduplicatedRefresh() to prevent racing with other
      // refresh calls (e.g. visibility change, getAccessToken).
      if (session) {
        const now = Math.floor(Date.now() / 1000);
        const expiresAt = session.expires_at ?? 0;
        if (expiresAt - now < 60) {
          updateLocalExpiry(session); // set _localExpiresAt so dedup knows the state
          const freshToken = await deduplicatedRefresh(set);
          if (freshToken) {
            session = get().session; // pull the updated session from store
          } else {
            session = null;
          }
        }
      }

      updateLocalExpiry(session);
      set({
        session,
        user: session?.user ?? null,
        loading: false,
        initialized: true,
        emailVerified: isEmailVerified(session?.user ?? null),
      });
      // Schedule proactive refresh
      if (session) {
        scheduleProactiveRefresh(set);
      }
      // Check onboarding status if user is logged in
      if (session?.user) {
        onboardingCheckTriggered = true;
        get().checkOnboardingStatus();
      } else {
        set({ onboardingChecked: true });
      }
    });

    // Subscribe to auth state changes.
    // IMPORTANT: INITIAL_SESSION fires synchronously during subscription setup,
    // BEFORE getSession().then() has a chance to refresh expired tokens.
    // INITIAL_SESSION must NOT set initialized=true or fire API calls —
    // getSession().then() handles that after verifying/refreshing the token.
    const {
      data: { subscription },
    } = authClient.auth.onAuthStateChange((event, session) => {
      // Skip INITIAL_SESSION entirely — getSession().then() handles cold-load
      // initialization with proper token refresh logic.
      if (event === "INITIAL_SESSION") return;

      // A SIGNED_OUT during backoff came from a failed refreshSession(), not
      // a genuine logout. Suppress it so ProtectedRoute doesn't bounce the
      // user to /login while the recovery timer is still going to retry.
      // The recovery timer will attempt a fresh refresh after cooldown.
      if ((event === "SIGNED_OUT" || !session?.user) && Date.now() < _refreshBackoffUntil && get().user) {
        return;
      }

      updateLocalExpiry(session);
      set({
        session,
        user: session?.user ?? null,
        loading: false,
        initialized: true,
        emailVerified: isEmailVerified(session?.user ?? null),
      });
      // Re-schedule proactive refresh on every session change
      scheduleProactiveRefresh(set);

      if (event === "SIGNED_IN" && session?.user && !get().onboardingChecked && !onboardingCheckTriggered) {
        // Check onboarding with the fresh token.
        onboardingCheckTriggered = true;
        get().checkOnboardingStatus();
      } else if (event === "SIGNED_OUT" || !session?.user) {
        set({ onboarded: false, onboardingChecked: true });
      }
    });

    // When the tab regains focus (after sleep/background), check token
    // immediately — setTimeout is throttled by Chrome in background tabs,
    // so the proactive refresh timer may not have fired.
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible" && get().session) {
        const now = Math.floor(Date.now() / 1000);
        if (_localExpiresAt && _localExpiresAt - now < 120) {
          deduplicatedRefresh(set).then((token) => {
            if (token) scheduleProactiveRefresh(set);
          });
        }
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    // Return the unsubscribe function for cleanup
    return () => {
      subscription.unsubscribe();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (_refreshTimer) {
        clearTimeout(_refreshTimer);
        _refreshTimer = null;
      }
      if (_recoveryTimer) {
        clearTimeout(_recoveryTimer);
        _recoveryTimer = null;
      }
    };
  },

  checkOnboardingStatus: async () => {
    try {
      // Use the api wrapper which has built-in 401 retry with token refresh,
      // rather than raw fetch() which would fail silently on expired tokens.
      const { api } = await import("@/lib/api");
      const data = await api.get<{ complete: boolean }>("/api/onboarding/status");
      set({ onboarded: data.complete === true, onboardingChecked: true });
    } catch {
      // If the check fails (network error, auth failure after retry, etc.),
      // default to onboarded=true so existing users aren't incorrectly sent
      // to onboarding due to a transient error. The dashboard's setup
      // checklist will guide users who genuinely need setup.
      set({ onboarded: true, onboardingChecked: true });
    }
  },

  checkEmailVerification: async () => {
    try {
      const { data, error } = await authClient.auth.getUser();
      if (error || !data.user) return;
      set({ emailVerified: isEmailVerified(data.user) });
    } catch {
      // Non-fatal — polling will retry
    }
  },

  signInWithEmail: async (email: string, password: string) => {
    set({ loading: true });
    const { data, error } = await authClient.auth.signInWithPassword({
      email,
      password,
    });
    if (error) {
      set({ loading: false });
      throw error;
    }
    // Update local expiry with the fresh session from sign-in response
    updateLocalExpiry(data.session);
    // Eagerly set session/user so isAuthenticated is true immediately,
    // rather than waiting for the async onAuthStateChange listener.
    set({
      session: data.session,
      user: data.session?.user ?? null,
      loading: false,
      initialized: true,
    });
    // Schedule proactive refresh for the new session
    scheduleProactiveRefresh(set);
    // Immediately check onboarding with the fresh token from sign-in response.
    // This uses the known-good token, avoiding any race with onAuthStateChange.
    if (data.session?.user) {
      get().checkOnboardingStatus();
    }
  },

  signUp: async (email: string, password: string) => {
    set({ loading: true });

    // Pre-signup: enforce password complexity (client-side for instant feedback)
    const { validateNewPassword } = await import("@/lib/password-validation");
    const pwError = validateNewPassword(password);
    if (pwError) {
      set({ loading: false });
      throw new Error(pwError);
    }

    // Signup is a server call rather than a client-SDK one because the server
    // also screens disposable domains and reclaims squatted unconfirmed
    // addresses — see server/api/auth_validation.py.
    const resp = await fetch("/api/auth/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const result = await resp.json();

    if (!resp.ok) {
      set({ loading: false });
      const errorMsg = result.error || "Account creation failed. Please try again.";
      const err = new Error(errorMsg);
      // Preserve status code so Login.tsx can detect 429s for cooldown
      (err as { status?: number }).status = resp.status;
      throw err;
    }

    // With AUTH_AUTOCONFIRM on (the default, because a self-hosted instance
    // normally has no mail transport) the server hands back a live session.
    // Adopt it so the user lands signed in rather than being told to check an
    // inbox that will never receive anything.
    if (result.session) {
      authClient.auth.adoptSession(result.session);
      updateLocalExpiry(result.session);
      set({
        session: result.session,
        user: result.session.user ?? null,
        loading: false,
        initialized: true,
        emailVerified: isEmailVerified(result.session.user ?? null),
      });
      scheduleProactiveRefresh(set);
      get().checkOnboardingStatus();
      return {};
    }

    // Confirmation required — the caller shows the "check your email" state.
    set({ loading: false });
    return { confirmUrl: result.confirm_url };
  },

  signOut: async () => {
    if (_refreshTimer) {
      clearTimeout(_refreshTimer);
      _refreshTimer = null;
    }
    if (_recoveryTimer) {
      clearTimeout(_recoveryTimer);
      _recoveryTimer = null;
    }
    _refreshBackoffUntil = 0;
    // Best-effort sign-out: if the server rejects it (e.g. a 403 because the
    // account no longer exists), local state is cleared regardless.
    try {
      await authClient.auth.signOut();
    } catch {
      // Network error or a throwing client — ignore and clear local state below.
    }
    set({ user: null, session: null, onboarded: false, onboardingChecked: false, emailVerified: true, rateLimitedUntil: 0 });
  },

  getAccessToken: async () => {
    const { session } = get();
    if (!session) return null;

    // Pre-expiry refresh is normally handled by scheduleProactiveRefresh() and
    // the visibility-change handler — they fire ~60s before expiry.
    //
    // A tiny 5s buffer here catches the edge case where a burst of parallel
    // requests (e.g. refetchAfterReconciliation firing 6 queries at once) lands
    // in the last seconds of the token's life. Without the buffer, the server
    // returns 401 and the browser logs it before the retry succeeds — spamming
    // the console even though the app recovers. deduplicatedRefresh() collapses
    // the concurrent calls into a single refresh, so contention is not a concern.
    if (_localExpiresAt) {
      const now = Math.floor(Date.now() / 1000);
      if (now >= _localExpiresAt - 5) {
        const refreshed = await deduplicatedRefresh(set);
        return refreshed ?? session.access_token;
      }
    }

    return session.access_token;
  },

  forceRefreshToken: async () => {
    // During backoff, return the current token rather than null.
    // This lets 401-retry use the existing (possibly still valid) token
    // instead of failing and potentially triggering more refresh attempts.
    const refreshed = await deduplicatedRefresh(set);
    if (refreshed) return refreshed;
    return get().session?.access_token ?? null;
  },

  setOnboarded: (value: boolean) => {
    set({ onboarded: value, onboardingChecked: true });
  },
}));
