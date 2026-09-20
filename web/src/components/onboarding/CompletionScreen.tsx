import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { CheckCircle2, ArrowRight, Receipt, BarChart3, Landmark, Loader2, Play } from "lucide-react";
import { api } from "@/lib/api";

interface CompletionScreenProps {
  companyName: string;
  onGoToDashboard: () => void;
  hasFiles?: boolean;
}

export function CompletionScreen({
  companyName,
  onGoToDashboard,
  hasFiles,
}: CompletionScreenProps) {
  const navigate = useNavigate();
  const [isMatching, setIsMatching] = useState(false);
  const [matchError, setMatchError] = useState<string | null>(null);

  async function handleMatchTransactions() {
    setIsMatching(true);
    setMatchError(null);
    try {
      await api.post("/api/reconciliation/start");
      // Navigate to transactions page after starting reconciliation
      navigate("/transactions", { replace: true });
    } catch (err: unknown) {
      const error = err as { message?: string };
      setMatchError(error.message || "Could not start matching. You can try again from the dashboard.");
      setIsMatching(false);
    }
  }

  return (
    <div className="space-y-8 text-center">
      {/* Large check icon */}
      <div className="mx-auto flex h-20 w-20 items-center justify-center rounded-full bg-primary/10">
        <CheckCircle2 className="h-12 w-12 text-primary" />
      </div>

      {/* Heading */}
      <div>
        <h2 className="text-3xl font-bold tracking-tight">
          You're all set, {companyName}!
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Your account is ready. Here's what you can do next:
        </p>
      </div>

      {/* Next steps */}
      <div className="mx-auto max-w-sm space-y-3 text-left">
        <div
          className="flex items-start gap-3 rounded-xl border border-primary/10 bg-primary/[0.02] p-4 shadow-warm-sm card-hover cursor-pointer"
          role="button"
          tabIndex={0}
          onClick={() => navigate("/transactions?upload=1")}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); navigate("/transactions?upload=1"); } }}
        >
          <Receipt className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <div>
            <p className="text-sm font-medium">Upload receipts & invoices</p>
            <p className="text-xs text-muted-foreground">
              Drag and drop PDFs, photos, or screenshots of your receipts and invoices
            </p>
          </div>
        </div>

        <div
          className="flex items-start gap-3 rounded-xl border border-primary/10 bg-primary/[0.02] p-4 shadow-warm-sm card-hover cursor-pointer"
          role="button"
          tabIndex={0}
          onClick={() => navigate("/transactions?upload=1")}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); navigate("/transactions?upload=1"); } }}
        >
          <Landmark className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <div>
            <p className="text-sm font-medium">Import bank statements</p>
            <p className="text-xs text-muted-foreground">
              Upload CSV or PDF bank statements from any bank or financial institution
            </p>
          </div>
        </div>

        <div
          className="flex items-start gap-3 rounded-xl border border-primary/10 bg-primary/[0.02] p-4 shadow-warm-sm card-hover cursor-pointer"
          role="button"
          tabIndex={0}
          onClick={() => navigate("/review")}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); navigate("/review"); } }}
        >
          <BarChart3 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <div>
            <p className="text-sm font-medium">Review matched transactions</p>
            <p className="text-xs text-muted-foreground">
              See how your receipts match to bank transactions with confidence scores
            </p>
          </div>
        </div>
      </div>

      {/* You're in control */}
      <div className="rounded-xl border border-primary/20 bg-primary/5 p-4 text-left space-y-2 mx-auto max-w-sm">
        <p className="text-sm font-semibold text-primary">You're in control</p>
        <p className="text-xs text-muted-foreground leading-relaxed">
          Receipts are matched to bank transactions automatically. You review every match
          before it goes into your final report — approve, reject, or reassign with one click.
        </p>
      </div>

      {/* Match error */}
      {matchError && (
        <p className="text-sm text-destructive">{matchError}</p>
      )}

      {/* Primary CTA: conditional based on upload state */}
      <div className="space-y-3">
        {hasFiles ? (
          <>
            <Button
              onClick={handleMatchTransactions}
              disabled={isMatching}
              size="lg"
              className="gap-2 bg-primary hover:bg-primary/90 shadow-warm"
            >
              {isMatching ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Play className="h-4 w-4" />
              )}
              {isMatching ? "Starting..." : "Match My Transactions"}
            </Button>

            {/* Secondary: Go to dashboard */}
            <div>
              <Button
                onClick={onGoToDashboard}
                variant="ghost"
                className="gap-2 text-muted-foreground hover:text-foreground"
              >
                Go to Dashboard
                <ArrowRight className="h-4 w-4" />
              </Button>
            </div>
          </>
        ) : (
          <Button
            onClick={onGoToDashboard}
            size="lg"
            className="gap-2 bg-primary hover:bg-primary/90 shadow-warm"
          >
            Go to Dashboard
            <ArrowRight className="h-4 w-4" />
          </Button>
        )}
      </div>

      {/* Regulatory disclaimer */}
      <p className="text-xs text-muted-foreground/70">
        Autonomous Accounting organizes your records around CRA record-keeping guidance. That is not a certification, and tax filing remains your responsibility.
      </p>
    </div>
  );
}
