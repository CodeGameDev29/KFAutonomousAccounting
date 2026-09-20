import { useAuthStore } from "@/stores/auth-store";
import { API_URL } from "./constants";

class ApiError extends Error {
  status: number;
  detail: string;
  /** Structured error code from the backend (e.g. "LOCAL_MODEL_UNREACHABLE") */
  code?: string;

  constructor(status: number, detail: string | Record<string, unknown>) {
    const msg =
      typeof detail === "string"
        ? detail
        : (detail.message as string) || `API error: ${status}`;
    super(msg);
    this.name = "ApiError";
    this.status = status;
    this.detail = msg;
    if (typeof detail === "object" && detail !== null) {
      this.code = detail.code as string | undefined;
    }
  }
}

/**
 * Build the headers for a request, merging caller-provided headers on top of
 * the default JSON + Authorization headers.
 */
function buildHeaders(
  token: string | null,
  callerHeaders?: HeadersInit
): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };

  if (callerHeaders) {
    const entries = callerHeaders as Record<string, string>;
    Object.entries(entries).forEach(([key, value]) => {
      if (value === "") {
        delete headers[key];
      } else {
        headers[key] = value;
      }
    });
  }

  return headers;
}

/**
 * Execute a single fetch request with the given token.
 * Returns the raw Response so the caller can inspect status before parsing.
 */
async function doFetch(
  path: string,
  token: string | null,
  options?: RequestInit
): Promise<Response> {
  const headers = buildHeaders(token, options?.headers);
  return fetch(`${API_URL}${path}`, {
    ...options,
    headers,
  });
}

/**
 * Parse a successful response, handling 204 No Content.
 */
async function parseResponse<T>(res: Response): Promise<T> {
  if (res.status === 204) {
    return undefined as T;
  }
  return res.json();
}

/**
 * Throw an ApiError from a failed response.
 */
async function throwApiError(res: Response): Promise<never> {
  const error = await res.json().catch(() => ({ detail: res.statusText }));
  throw new ApiError(
    res.status,
    error.detail || `API error: ${res.status}`
  );
}

/**
 * Core fetch wrapper with automatic 401 retry.
 *
 * On a 401 response the function force-refreshes the access token and
 * retries the request exactly once.  If the retry also returns 401 (or the
 * refresh itself fails) the error is thrown normally.
 *
 * FormData bodies are safe to re-send because the underlying File/Blob
 * references remain readable after the first fetch — the browser does not
 * consume them.  We therefore pass the *same* options (including body) to the
 * retry call.
 */
async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const { getAccessToken, forceRefreshToken } = useAuthStore.getState();
  const token = await getAccessToken();

  const res = await doFetch(path, token, options);

  if (res.status === 401) {
    // Token may have expired mid-flight — force a refresh and retry once.
    // IMPORTANT: Do NOT call signOut() here. With concurrent requests (e.g.,
    // bulk upload), one failed refresh would destroy the session for ALL
    // in-flight requests, causing a cascade of failures.
    const freshToken = await forceRefreshToken();
    if (freshToken) {
      const retryRes = await doFetch(path, freshToken, options);
      if (!retryRes.ok) {
        await throwApiError(retryRes);
      }
      return parseResponse<T>(retryRes);
    }
    // Refresh failed — throw the 401 error. The caller (mutation onError)
    // handles the failure for this individual request. The auth state
    // listener in auth-store.ts will detect session expiry via the auth
    // onAuthStateChange and redirect to login if truly expired.
  }

  if (!res.ok) {
    await throwApiError(res);
  }

  return parseResponse<T>(res);
}

export const api = {
  get: <T>(path: string) => apiFetch<T>(path),

  post: <T>(path: string, data?: unknown) =>
    apiFetch<T>(path, {
      method: "POST",
      body: data ? JSON.stringify(data) : undefined,
    }),

  put: <T>(path: string, data?: unknown) =>
    apiFetch<T>(path, {
      method: "PUT",
      body: data ? JSON.stringify(data) : undefined,
    }),

  patch: <T>(path: string, data?: unknown) =>
    apiFetch<T>(path, {
      method: "PATCH",
      body: data ? JSON.stringify(data) : undefined,
    }),

  delete: <T>(path: string) =>
    apiFetch<T>(path, { method: "DELETE" }),

  upload: <T>(path: string, formData: FormData) =>
    apiFetch<T>(path, {
      method: "POST",
      body: formData,
      headers: { "Content-Type": "" }, // Let browser set multipart boundary
    }),
};

export { ApiError };
