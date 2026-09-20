/**
 * Shared download utility — handles both signed URL JSON responses
 * and direct binary downloads with JWT auth and 401 retry.
 */

import { useAuthStore } from "@/stores/auth-store";
import { API_URL } from "@/lib/constants";

/**
 * Mint a short-lived, single-use download ticket.
 *
 * Hits POST /api/files/ticket with the JWT in the Authorization header and
 * returns an opaque ticket string valid for ~60 seconds. The ticket is
 * scoped to file downloads, tied to the current user, and dropped from the
 * server's vault on first consume.
 */
export async function mintDownloadTicket(): Promise<string> {
  const token = await useAuthStore.getState().getAccessToken();
  if (!token) throw new Error("Not authenticated");

  const res = await fetch(`${API_URL}/api/files/ticket`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("Failed to mint download ticket");
  const data = (await res.json()) as { ticket: string };
  if (!data.ticket) throw new Error("Malformed ticket response");
  return data.ticket;
}

/**
 * Trigger a native browser download via ``<a href>`` navigation.
 *
 * The browser fetches the URL directly (not via fetch API), so it honors
 * the server's ``Content-Disposition`` header to name the saved file. This
 * produces proper filenames in the browser's download history — unlike the
 * blob URL + ``a.download`` approach, which Chrome often saves as a UUID.
 *
 * Uses a single-use ticket (``?ticket=``) rather than the raw JWT; the
 * ticket never grants more than 60 seconds of download authority and is
 * consumed on use, so it is safe to appear in browser history / proxy logs.
 */
export async function triggerNativeDownload(path: string): Promise<void> {
  const ticket = await mintDownloadTicket();

  const sep = path.includes("?") ? "&" : "?";
  const url = `${API_URL}${path}${sep}ticket=${encodeURIComponent(ticket)}`;

  const a = document.createElement("a");
  a.href = url;
  // Hint to the browser that this is a download, not a navigation. The actual
  // filename comes from the server's Content-Disposition header.
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export async function downloadBinaryOrUrl(
  path: string,
  method: "GET" | "POST",
  token: string | null,
  fallbackFilename: string
): Promise<void> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  let res = await fetch(`${API_URL}${path}`, { method, headers });

  // Retry on 401 with refreshed token
  if (res.status === 401) {
    const freshToken = await useAuthStore.getState().forceRefreshToken();
    if (freshToken) {
      headers.Authorization = `Bearer ${freshToken}`;
      res = await fetch(`${API_URL}${path}`, { method, headers });
    }
  }

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const detail =
      typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
    const e = new Error(detail || "Download failed") as Error & {
      status?: number;
    };
    e.status = res.status;
    throw e;
  }

  const ct = res.headers.get("content-type") || "";

  // JSON response with signed download URL
  if (ct.includes("application/json")) {
    const data = await res.json();
    if (data.url) {
      // Fetch the signed URL as a blob so the href is a same-origin object URL.
      // Cross-origin hrefs cause the browser to ignore the download attribute,
      // resulting in hashed filenames from the storage path.
      const blobRes = await fetch(data.url);
      const blob = await blobRes.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = data.filename || fallbackFilename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(blobUrl);
      return;
    }
  }

  // Direct binary download
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition");
  let filename = fallbackFilename;
  if (cd) {
    const match = cd.match(/filename="?([^";\n]+)"?/);
    if (match) filename = match[1];
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
