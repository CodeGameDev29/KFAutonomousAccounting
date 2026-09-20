import type { AuthError } from "./auth-client";

/**
 * Translates an error from the local auth service into a message worth showing
 * a user.
 *
 * Auth is a same-origin API (`server/local_auth.py`), so an error arrives as
 * either an `AuthError` object (the server answered with a non-2xx and a JSON
 * body) or a thrown `Error`/`TypeError` (the request never completed). The two
 * cases want different words: the first is usually the user's problem to fix,
 * the second is almost always that the server is not reachable.
 */

type Context = "signin" | "signup" | "forgot" | "reset";

function isAuthError(err: unknown): err is AuthError {
  return (
    typeof err === "object" &&
    err !== null &&
    "message" in err &&
    typeof (err as AuthError).message === "string"
  );
}

function isNetworkFailure(err: unknown): boolean {
  if (err instanceof TypeError) return true;
  const msg = err instanceof Error ? err.message.toLowerCase() : "";
  return msg.includes("failed to fetch") || msg.includes("networkerror");
}

export function getFriendlyAuthError(err: unknown, context?: Context): string {
  // --- The request never reached the server ---
  // Self-hosted, this nearly always means the server is not running, the
  // machine it is on is asleep, or whatever exposes it is down — so say
  // something that points at that rather than at the user's password.
  if (isNetworkFailure(err)) {
    return "Can't reach Autonomous Accounting right now. The server may be restarting — please try again in a moment.";
  }

  if (isAuthError(err)) {
    const msg = err.message ?? "";
    const status = err.status;
    const lower = msg.toLowerCase();

    if (status === 502 || status === 503 || status === 504) {
      return "Autonomous Accounting is temporarily unavailable. Please try again in a moment.";
    }
    if (status === 429) {
      return "Too many attempts. Please wait a moment and try again.";
    }
    if (lower.includes("invalid login credentials")) {
      return "Incorrect email or password. Please check your credentials and try again.";
    }
    if (lower.includes("email not confirmed")) {
      return "Please check your email and click the confirmation link before signing in.";
    }
    if (lower.includes("already registered") || lower.includes("already exists")) {
      return "An account with this email already exists. Try signing in instead.";
    }
    if (lower.includes("invalid or expired refresh token")) {
      return "Your session has expired. Please sign in again.";
    }
    if (lower.includes("reset link is invalid") || lower.includes("expired")) {
      if (context === "reset" || context === "forgot") {
        return "Your password reset link has expired or was already used. Request a new one and try again.";
      }
      return "Your session has expired. Please sign in again.";
    }

    // Server-authored messages are written for users; pass them through.
    if (msg && msg !== "null" && msg !== "undefined") return msg;
  }

  if (err instanceof Error && err.message && err.message !== "undefined") {
    return err.message;
  }

  const contextMessages: Record<string, string> = {
    signin: "Sign-in failed. Please try again.",
    signup: "Account creation failed. Please try again.",
    forgot: "Failed to send reset link. Please try again.",
    reset: "Failed to update password. Please try again.",
  };
  return (context && contextMessages[context]) || "Authentication failed. Please try again.";
}
