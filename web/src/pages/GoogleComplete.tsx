import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/button";
import { authClient } from "@/lib/auth-client";
import { useAuthStore } from "@/stores/auth-store";

/**
 * Landing point after Google sends the browser back.
 *
 * The server put a single-use ticket in the URL rather than the session tokens
 * themselves, so nothing reusable ends up in browser history or a Referer
 * header. This page trades that ticket for a real session and then gets out of
 * the way.
 */
export function GoogleComplete() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  // React 18 StrictMode mounts effects twice in development. The ticket is
  // single-use, so a second exchange would fail and show a spurious error.
  const exchanged = useRef(false);

  useEffect(() => {
    if (exchanged.current) return;
    exchanged.current = true;

    const ticket = params.get("ticket");
    if (!ticket) {
      setError("This sign-in link is incomplete. Please try again.");
      return;
    }

    authClient.auth.completeGoogleSignIn(ticket).then(({ data, error: err }) => {
      if (err || !data.session) {
        setError(err?.message ?? "Sign-in could not be completed.");
        return;
      }
      // Bring the store in step with the session the client just adopted, so
      // ProtectedRoute sees an authenticated user immediately.
      useAuthStore.setState({
        session: data.session,
        user: data.session.user,
        loading: false,
        initialized: true,
        emailVerified: true,
      });
      useAuthStore.getState().checkOnboardingStatus();
      navigate("/dashboard", { replace: true });
    });
  }, [params, navigate]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm rounded-2xl border border-border bg-card p-8 text-center shadow-lg">
        <Logo className="mx-auto mb-4 h-10 w-10" />
        {error ? (
          <>
            <h1 className="mb-2 text-xl font-semibold text-foreground">
              Couldn&apos;t finish signing in
            </h1>
            <p className="mb-6 text-sm text-muted-foreground">{error}</p>
            <Button className="w-full" onClick={() => navigate("/login", { replace: true })}>
              Back to sign in
            </Button>
          </>
        ) : (
          <>
            <h1 className="mb-2 text-xl font-semibold text-foreground">Signing you in…</h1>
            <p className="text-sm text-muted-foreground">One moment.</p>
          </>
        )}
      </div>
    </div>
  );
}
