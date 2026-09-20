import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AlertTriangle } from "lucide-react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Plain wording for a crash. Nothing here can point at a support desk — the
 * software has no vendor — so anything that is not the user's to fix points at
 * the server and, when they run it themselves, at its log.
 */
function getDiagnosticHint(error: Error | null): string {
  if (!error) return "An unexpected error occurred. Please reload the page.";
  const msg = error.message?.toLowerCase() ?? "";
  if (msg.includes("network") || msg.includes("fetch") || msg.includes("failed to fetch")) {
    return "Could not reach the server. It may be stopped or restarting; if you run this instance, check that it is up.";
  }
  if (msg.includes("401") || msg.includes("unauthorized") || msg.includes("session")) {
    return "Your session may have expired. Please sign in again.";
  }
  if (msg.includes("403") || msg.includes("forbidden")) {
    return "You don't have permission to access this resource.";
  }
  if (msg.includes("404") || msg.includes("not found")) {
    return "The requested data could not be found. It may have been deleted or moved.";
  }
  if (msg.includes("429") || msg.includes("rate limit") || msg.includes("too many")) {
    return "Too many requests. Please wait a moment and try again.";
  }
  if (msg.includes("500") || msg.includes("server error") || msg.includes("internal")) {
    return "The server encountered an error. This is usually temporary — please try again in a moment.";
  }
  if (msg.includes("timeout") || msg.includes("timed out")) {
    return "The request took too long. This may be due to a large dataset — please try again.";
  }
  return "An unexpected error occurred. If it keeps happening and you run this instance, the server log and the browser console have the detail.";
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;

      return (
        <div className="flex min-h-[400px] items-center justify-center p-8">
          <Card className="max-w-md shadow-warm rounded-xl">
            <CardHeader className="text-center">
              <div className="mx-auto mb-2 flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10">
                <AlertTriangle className="h-6 w-6 text-destructive" />
              </div>
              <CardTitle>Something went wrong</CardTitle>
            </CardHeader>
            <CardContent className="text-center">
              <p className="mb-4 text-sm text-muted-foreground">
                {getDiagnosticHint(this.state.error)}
              </p>
              <Button
                onClick={() => {
                  this.setState({ hasError: false, error: null });
                  window.location.reload();
                }}
              >
                Reload Page
              </Button>
            </CardContent>
          </Card>
        </div>
      );
    }

    return this.props.children;
  }
}
