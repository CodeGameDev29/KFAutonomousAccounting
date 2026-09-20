export const API_URL = (() => {
  // An empty string means relative URLs (same-origin), which is right in both
  // modes:
  //   - development: the Vite dev server proxies /api to the backend port;
  //   - a built bundle: FastAPI serves the SPA and the API from one origin, so
  //     relative /api/* requests reach the backend however the instance is
  //     exposed.
  // VITE_API_URL can override it for a split deployment, but an empty value is
  // the norm.
  return import.meta.env.VITE_API_URL || "";
})();

/**
 * Auth is served by this same origin (`/api/auth/*`, see server/local_auth.py),
 * so there is nothing to configure and nothing that can be half-configured.
 *
 * The constant is `true` rather than absent so that the guard clauses reading
 * it stay in place should auth ever move off-origin, where a missing base URL
 * or key would once again be a real failure mode.
 */
export const AUTH_CONFIGURED = true;

/**
 * Whether registration is open is NOT a constant here.
 *
 * It is a per-instance setting (`AUTH_ALLOW_SIGNUP`, closed by default) that
 * the server enforces on `POST /api/auth/signup`. Baked into the bundle it
 * would describe whichever instance produced the build rather than the one
 * serving the page, so the sign-up page reads it from the server instead:
 * `useSignupEnabled()` in `@/hooks/use-system-info`.
 */

export const APP_NAME = "Autonomous Accounting";

export const ROUTES = {
  LANDING: "/",
  LOGIN: "/login",
  ONBOARDING: "/onboarding",
  DASHBOARD: "/dashboard",
  TRANSACTIONS: "/transactions",
  REPORTS: "/reports",
  REVIEW: "/review",
  SETTINGS: "/settings",
  SECURITY: "/settings?tab=security",
  SHARED_REPORT: "/shared/:token",
} as const;
