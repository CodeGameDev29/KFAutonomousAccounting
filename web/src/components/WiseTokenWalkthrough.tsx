import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ExternalLink,
  Key,
  Copy,
  ClipboardCheck,
  ArrowLeft,
  ArrowRight,
  CheckCircle,
  XCircle,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface WiseTokenWalkthroughProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess?: () => void;
}

const STEPS = [
  {
    title: "Open Wise API Tokens",
    description:
      "Open the API tokens page in Wise — the button below goes straight there.",
  },
  {
    title: "Create a Token",
    description:
      'On the API tokens page, click "Add new token". Choose Read only access, give it a name, and create it.',
  },
  {
    title: "Copy Your Token",
    description:
      "Once the token is created, click Reveal key to see it, then copy it. You will only see it once.",
  },
  {
    title: "Paste and Verify",
    description:
      "Paste your Wise API token below; the server verifies it before saving.",
  },
];

const WISE_SETTINGS_URL = "https://wise.com/your-account/integrations-and-tools/api-tokens";

export function WiseTokenWalkthrough({
  open,
  onOpenChange,
  onSuccess,
}: WiseTokenWalkthroughProps) {
  const [step, setStep] = useState(0);
  const [token, setToken] = useState("");
  const [verifyStatus, setVerifyStatus] = useState<
    "idle" | "success" | "error"
  >("idle");

  const verifyMutation = useMutation({
    mutationFn: (apiToken: string) =>
      api.post<{ status: string; profile_id: number }>("/api/onboarding/wise/test", {
        token: apiToken,
      }),
    onSuccess: () => {
      setVerifyStatus("success");
    },
    onError: () => {
      setVerifyStatus("error");
    },
  });

  function handleVerify() {
    if (!token.trim()) return;
    setVerifyStatus("idle");
    verifyMutation.mutate(token.trim());
  }

  function handleDone() {
    setStep(0);
    setToken("");
    setVerifyStatus("idle");
    onOpenChange(false);
    onSuccess?.();
  }

  function handleClose() {
    setStep(0);
    setToken("");
    setVerifyStatus("idle");
    onOpenChange(false);
  }

  const canGoNext = step < STEPS.length - 1;
  const canGoBack = step > 0;
  const isLastStep = step === STEPS.length - 1;

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-md shadow-warm-md rounded-xl">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold">{STEPS[step].title}</DialogTitle>
          <DialogDescription>{STEPS[step].description}</DialogDescription>
        </DialogHeader>

        {/* Step indicators */}
        <div className="flex items-center justify-center gap-2 py-2">
          {STEPS.map((_, i) => (
            <div
              key={i}
              className={cn(
                "h-2 w-2 rounded-full transition-colors",
                i === step
                  ? "bg-primary"
                  : i < step
                    ? "bg-primary/40"
                    : "bg-muted"
              )}
            />
          ))}
        </div>

        {/* Step content */}
        <div className="py-2">
          {step === 0 && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
                <ExternalLink className="h-8 w-8 text-primary" />
              </div>
              <Button
                variant="outline"
                className="gap-2 border-primary/20 text-primary hover:bg-primary/5"
                onClick={() => window.open(WISE_SETTINGS_URL, "_blank")}
              >
                Open Wise API Tokens
                <ExternalLink className="h-4 w-4" />
              </Button>
            </div>
          )}

          {step === 1 && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
                <Key className="h-8 w-8 text-primary" />
              </div>
              <div className="rounded-xl border bg-muted/20 p-4 text-sm text-muted-foreground shadow-warm-sm">
                <ol className="space-y-2">
                  <li>
                    1. On the <strong>API tokens</strong> page, click the green{" "}
                    <strong>Add new token</strong> button
                  </li>
                  <li>
                    2. Give it a name (e.g. &ldquo;AutonomousAccounting&rdquo;)
                  </li>
                  <li>
                    3. Select <strong>Read only</strong> access
                  </li>
                  <li>
                    4. Click <strong>Create token</strong>
                  </li>
                </ol>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-50">
                <Copy className="h-8 w-8 text-amber-600" />
              </div>
              <div className="rounded-xl border bg-muted/20 p-4 text-sm text-muted-foreground shadow-warm-sm">
                <p>
                  After creating the token, click{" "}
                  <strong>Reveal key</strong> to see it, then copy the
                  full API key to your clipboard.
                </p>
                <p className="mt-2 font-medium text-foreground">
                  Save it somewhere safe &mdash; you can always reveal it
                  again from this page, but it is easier to copy it now.
                </p>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-4">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10 mx-auto">
                <ClipboardCheck className="h-6 w-6 text-primary" />
              </div>
              <div className="space-y-2">
                <Label htmlFor="wise-walkthrough-token">Access Token</Label>
                <Input
                  id="wise-walkthrough-token"
                  type="password"
                  placeholder="Paste your Wise access token here"
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value);
                    setVerifyStatus("idle");
                  }}
                  className="font-mono focus-visible:ring-primary"
                />
              </div>
              <div className="flex items-center gap-3">
                <Button
                  onClick={handleVerify}
                  disabled={!token.trim() || verifyMutation.isPending}
                  className="gap-2 bg-primary hover:bg-primary/90"
                >
                  {verifyMutation.isPending && (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  )}
                  Test Connection
                </Button>
                {verifyStatus === "success" && (
                  <span className="flex items-center gap-1 text-sm text-primary">
                    <CheckCircle className="h-4 w-4" />
                    Verified
                  </span>
                )}
                {verifyStatus === "error" && (
                  <span className="flex items-center gap-1 text-sm text-rose-600">
                    <XCircle className="h-4 w-4" />
                    Failed &mdash; check your token
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="flex-row justify-between sm:justify-between">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setStep((s) => s - 1)}
            disabled={!canGoBack}
            className="gap-1 text-muted-foreground"
          >
            <ArrowLeft className="h-4 w-4" />
            Back
          </Button>

          {isLastStep ? (
            <Button
              size="sm"
              onClick={handleDone}
              disabled={verifyStatus !== "success"}
              className="gap-1 bg-primary hover:bg-primary/90"
            >
              Done
              <CheckCircle className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              size="sm"
              onClick={() => setStep((s) => s + 1)}
              disabled={!canGoNext}
              className="gap-1 bg-primary hover:bg-primary/90"
            >
              Next
              <ArrowRight className="h-4 w-4" />
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
