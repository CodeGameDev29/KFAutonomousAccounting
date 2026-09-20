/**
 * User-facing wording for a failed request.
 *
 * Two rules hold everywhere in here. Nothing speaks for a vendor: there is no
 * company behind this software and no support desk to write to, so a failure
 * points at the server the user is signed in to and, when they run it
 * themselves, at its log. And no sentence states a number the bundle cannot
 * know: the upload limit is a per-instance setting the server reports on
 * `/health`, so it is either named from that or left out.
 */

/**
 * The server's upload limit, published by `useInstanceInfoSync()` once
 * `/health` has answered. Null means "not known yet" and the 413 wording
 * simply omits the number.
 */
let maxUploadMb: number | null = null;

export function setMaxUploadMb(mb: number | null): void {
  maxUploadMb = mb;
}

function tooLargeMessage(): string {
  return maxUploadMb === null
    ? "This file is larger than the limit this server accepts."
    : `This file is too large. This server accepts up to ${maxUploadMb} MB.`;
}

const GENERIC =
  "Something went wrong. Try again; if it keeps happening and you run this instance, check the server log.";

function genericMessage(context?: string): string {
  return context
    ? `Something went wrong while ${context}. Try again; if it keeps happening and you run this instance, check the server log.`
    : GENERIC;
}

export function getErrorMessage(error: unknown, context?: string): string {
  if (error instanceof TypeError && error.message.includes("fetch")) {
    // The server may be on the same machine as the browser, elsewhere on the
    // local network, or reachable over the internet — "check your internet
    // connection" is wrong in the first two cases and unhelpful in the third.
    return "Could not reach the server. It may be stopped or restarting; if you run this instance, check that it is up.";
  }
  if (error && typeof error === "object" && "status" in error) {
    const status = (error as { status: number }).status;
    switch (status) {
      case 400:
        return "The file format is not supported. Try JPEG, PNG, or PDF.";
      case 401:
        return "Your session has expired. Please log in again.";
      case 403:
        return "You don't have permission to access this resource.";
      case 404:
        return "The requested item could not be found.";
      case 413:
        return tooLargeMessage();
      case 429:
        return "Too many requests. Please wait a moment and try again.";
      case 500:
      case 502:
      case 503:
        return "The server is unavailable. If you run this instance, check the server log.";
      default:
        return genericMessage(context);
    }
  }
  return genericMessage(context);
}

export function getErrorMessageFromResponse(
  res: Response,
  context?: string
): string {
  return getErrorMessage({ status: res.status }, context);
}
