export type ErrorCategory = "network" | "auth" | "forbidden" | "not-found" | "rate-limit" | "server" | "unknown";

export interface ClassifiedError {
  category: ErrorCategory;
  title: string;
  message: string;
  retryable: boolean;
}

export function classifyError(error: unknown, context?: string): ClassifiedError {
  const label = context ? ` ${context}` : "";

  // Network errors (TypeError from fetch)
  if (error instanceof TypeError) {
    return {
      category: "network",
      title: "Connection problem",
      message: `Could not reach the server to load${label}. Check your internet connection and try again.`,
      retryable: true,
    };
  }

  // ApiError with status code (from web/src/lib/api.ts)
  if (error && typeof error === "object" && "status" in error) {
    const status = (error as { status: number }).status;
    if (status === 401)
      return { category: "auth", title: "Session expired",
               message: "Your session has expired. Please sign in again.", retryable: false };
    if (status === 403)
      return { category: "forbidden", title: "Access denied",
               message: `You don't have permission to view${label}.`, retryable: false };
    if (status === 404)
      return { category: "not-found", title: "Not found",
               message: `The requested${label} could not be found.`, retryable: false };
    if (status === 429)
      return { category: "rate-limit", title: "Too many requests",
               message: "Please wait a moment and try again.", retryable: true };
    if (status >= 500)
      return { category: "server", title: "Server error",
               message: `Something went wrong on the server while loading${label}. Please try again in a moment.`,
               retryable: true };
  }

  // Fallback string matching
  const msg = (error instanceof Error ? error.message : String(error)).toLowerCase();
  if (msg.includes("network") || msg.includes("failed to fetch") || msg.includes("timeout")) {
    return { category: "network", title: "Connection problem",
             message: `Could not reach the server to load${label}. Check your internet connection.`,
             retryable: true };
  }

  return { category: "unknown", title: "Something went wrong",
           message: `Could not load${label}. Please try again.`, retryable: true };
}
