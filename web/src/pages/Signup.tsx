import { useEffect } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useSignupEnabled } from "@/hooks/use-system-info";

/**
 * `/signup` and `/sign-up` route handler.
 *
 * `/signup` must resolve to real content rather than a SPA 404, but the app's
 * only signup form lives at `/login?mode=signup`. This component renders that
 * signup form by issuing a client-side `<Navigate>` to that URL — same browser
 * session, same React tree, no flash of 404, query params preserved across the
 * redirect.
 *
 * Whether it redirects at all depends on the instance: registration is closed
 * by default and the server is the only thing that knows whether it was opened
 * (`AUTH_ALLOW_SIGNUP`, reported on `/health`). While the answer is still in
 * flight the page says so rather than claiming registration is closed.
 *
 * Why not declare `/signup` as `element={<Login />}` directly? Login reads the
 * current pathname to set its mode, so forcing the user onto the `/login` URL
 * keeps that logic in one place and matches the Get-Started CTA across the app.
 */
export function Signup() {
  const location = useLocation();
  const signupEnabled = useSignupEnabled();

  // Preserve any incoming query params and merge mode=signup. A mode= the
  // caller already supplied wins; otherwise signup mode is forced so the form
  // opens to signup, not signin.
  const incoming = new URLSearchParams(location.search);
  if (!incoming.has("mode")) {
    incoming.set("mode", "signup");
  }
  const target = `/login?${incoming.toString()}`;

  // Render a meaningful H1 in the brief frame before <Navigate> swaps the
  // location, in case a crawler executes JS but halts before the navigation
  // completes. The H1 deliberately does not contain the string "404".
  useEffect(() => {
    document.title = "Sign up — Autonomous Accounting";
    const meta = document.createElement("meta");
    meta.name = "robots";
    meta.content = "noindex, follow";
    document.head.appendChild(meta);
    return () => { document.head.removeChild(meta); };
  }, []);

  return (
    <>
      {/* Visible-on-paint content so the crawler never sees an empty body. */}
      <main className="flex min-h-[40vh] flex-col items-center justify-center px-4 bg-background">
        <h1 className="text-3xl font-bold text-foreground">Sign up for Autonomous Accounting</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {signupEnabled === undefined
            ? "Checking whether this instance is accepting new accounts…"
            : signupEnabled
              ? "Redirecting to the signup form…"
              : "This instance is not accepting new accounts. Its operator can open registration by setting AUTH_ALLOW_SIGNUP=1 and restarting the server."}
        </p>
      </main>
      {signupEnabled === true && <Navigate to={target} replace />}
    </>
  );
}
