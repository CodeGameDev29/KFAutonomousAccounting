import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  ShoppingCart,
  Download,
  FileSearch,
  ExternalLink,
  ArrowLeft,
  ArrowRight,
  CheckCircle,
  Upload,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface AmazonImportWalkthroughProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess?: () => void;
}

const STEPS = [
  {
    title: "Choose Your Amazon Account Type",
    description:
      "Select the type of Amazon account you use for business purchases.",
  },
  {
    title: "Export Your Orders",
    description:
      "Follow the instructions to download your Amazon order data as a CSV file.",
  },
  {
    title: "Locate Your File",
    description: "Find the downloaded file on your computer.",
  },
  {
    title: "Upload Your CSV",
    description:
      "Use the Upload Statements button on the Transactions page to import your Amazon CSV.",
  },
];

const AMAZON_BUSINESS_URL =
  "https://www.amazon.ca/b2b/aba/dashboard?ref_=b2b_mcs_solutions_aba_hero";
const AMAZON_PRIVACY_URL =
  "https://www.amazon.ca/gp/privacycentral/dsar/preview.html";

export function AmazonImportWalkthrough({
  open,
  onOpenChange,
  onSuccess,
}: AmazonImportWalkthroughProps) {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [accountType, setAccountType] = useState<
    "business" | "personal" | null
  >(null);

  function handleGoToTransactions() {
    resetState();
    onOpenChange(false);
    onSuccess?.();
    navigate("/transactions");
  }

  function handleClose() {
    resetState();
    onOpenChange(false);
  }

  function resetState() {
    setStep(0);
    setAccountType(null);
  }

  const canGoNext =
    step < STEPS.length - 1 && (step !== 0 || accountType !== null);
  const canGoBack = step > 0;
  const isLastStep = step === STEPS.length - 1;

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-md shadow-warm-md rounded-xl">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold">
            {STEPS[step].title}
          </DialogTitle>
          <DialogDescription>{STEPS[step].description}</DialogDescription>
        </DialogHeader>

        {/* Step indicators */}
        <div
          className="flex items-center justify-center gap-2 py-2"
          role="progressbar"
          aria-valuenow={step + 1}
          aria-valuemax={STEPS.length}
        >
          {STEPS.map((s, i) => (
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
              aria-label={`Step ${i + 1} of ${STEPS.length}: ${s.title}`}
            />
          ))}
        </div>

        {/* Step content */}
        <div className="py-2">
          {/* Step 0: Account type selection */}
          {step === 0 && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
                <ShoppingCart className="h-8 w-8 text-primary" />
              </div>
              <p className="text-sm text-muted-foreground text-center">
                Which type of Amazon account do you use for business purchases?
              </p>
              <div
                className="grid gap-3 w-full"
                role="radiogroup"
                aria-label="Amazon account type"
              >
                <button
                  role="radio"
                  aria-checked={accountType === "business"}
                  onClick={() => setAccountType("business")}
                  className={cn(
                    "rounded-xl border p-4 text-left transition-colors",
                    accountType === "business"
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/30"
                  )}
                >
                  <p className="font-medium">Amazon Business</p>
                  <p className="text-sm text-muted-foreground">
                    business.amazon.ca — recommended for businesses
                  </p>
                </button>
                <button
                  role="radio"
                  aria-checked={accountType === "personal"}
                  onClick={() => setAccountType("personal")}
                  className={cn(
                    "rounded-xl border p-4 text-left transition-colors",
                    accountType === "personal"
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/30"
                  )}
                >
                  <p className="font-medium">Personal account</p>
                  <p className="text-sm text-muted-foreground">
                    amazon.ca — works for any account
                  </p>
                </button>
              </div>
              <p className="text-xs text-muted-foreground/80 italic mt-2">
                This software never signs in to Amazon. You export the data
                yourself and upload only the file you choose.
              </p>
            </div>
          )}

          {/* Step 1: Export instructions (conditional) */}
          {step === 1 && accountType === "business" && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
                <Download className="h-8 w-8 text-primary" />
              </div>
              <div className="rounded-xl border bg-muted/20 p-4 text-sm text-muted-foreground shadow-warm-sm">
                <p className="font-medium text-foreground mb-2">
                  Export from Business Analytics
                </p>
                <ol className="space-y-2">
                  <li>1. Open Business Analytics (link below)</li>
                  <li>
                    2. Click <strong>Reports</strong> &rarr;{" "}
                    <strong>Orders report</strong>
                  </li>
                  <li>
                    3. Set Order Date to <strong>Year to date</strong>
                  </li>
                  <li>
                    4. Click <strong>Generate report</strong>
                  </li>
                  <li>5. Download the CSV file when ready</li>
                </ol>
              </div>
              <Button
                variant="outline"
                className="gap-2 border-primary/20 text-primary hover:bg-primary/5"
                onClick={() => window.open(AMAZON_BUSINESS_URL, "_blank")}
              >
                Open Amazon Business Analytics
                <ExternalLink className="h-4 w-4" />
              </Button>
            </div>
          )}

          {step === 1 && accountType === "personal" && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
                <Download className="h-8 w-8 text-primary" />
              </div>
              <div className="rounded-xl border bg-muted/20 p-4 text-sm text-muted-foreground shadow-warm-sm">
                <p className="font-medium text-foreground mb-2">
                  Request Your Order Data
                </p>
                <ol className="space-y-2">
                  <li>
                    1. Open Amazon&apos;s data request page (link below)
                  </li>
                  <li>
                    2. Select <strong>Your Orders</strong> under the data
                    categories
                  </li>
                  <li>
                    3. Click <strong>Submit Request</strong>
                  </li>
                  <li>4. Check your email and confirm the request</li>
                  <li>
                    5. Wait for Amazon to prepare your download (usually a few
                    hours)
                  </li>
                </ol>
              </div>
              <Button
                variant="outline"
                className="gap-2 border-primary/20 text-primary hover:bg-primary/5"
                onClick={() => window.open(AMAZON_PRIVACY_URL, "_blank")}
              >
                Open Amazon Data Request
                <ExternalLink className="h-4 w-4" />
              </Button>
              <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-700">
                <p>
                  <strong>Note:</strong> Amazon may take a few hours to prepare
                  your data. You can close this dialog and come back when the
                  download is ready.
                </p>
              </div>
            </div>
          )}

          {/* Step 2: Locate file */}
          {step === 2 && accountType === "business" && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-50">
                <FileSearch className="h-8 w-8 text-amber-600" />
              </div>
              <div className="rounded-xl border bg-muted/20 p-4 text-sm text-muted-foreground shadow-warm-sm">
                <p>
                  Your downloaded file is a CSV named something like{" "}
                  <strong>purchase-report-2026.csv</strong>. It contains columns
                  for order date, item name, quantity, price, and tax.
                </p>
              </div>
            </div>
          )}

          {step === 2 && accountType === "personal" && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-50">
                <FileSearch className="h-8 w-8 text-amber-600" />
              </div>
              <div className="rounded-xl border bg-muted/20 p-4 text-sm text-muted-foreground shadow-warm-sm">
                <p className="mb-2">
                  Amazon sends a ZIP file. After downloading and unzipping it:
                </p>
                <ol className="space-y-2">
                  <li>1. Open the extracted folder</li>
                  <li>
                    2. Look for a file called{" "}
                    <strong>Retail.OrderHistory.1.csv</strong>
                  </li>
                  <li>
                    3. This is the file you will upload in the next step
                  </li>
                </ol>
                <p className="mt-2 text-muted-foreground/80 italic">
                  You can ignore the other files in the ZIP.
                </p>
              </div>
            </div>
          )}

          {/* Step 3: Go to Transactions to upload */}
          {step === 3 && (
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
                <Upload className="h-8 w-8 text-primary" />
              </div>
              <div className="rounded-xl border bg-muted/20 p-4 text-sm text-muted-foreground shadow-warm-sm text-center">
                <p>
                  Go to the <strong>Transactions</strong> page and click{" "}
                  <strong>Upload Statements</strong> to import your Amazon CSV
                  file.
                </p>
                <p className="mt-2 text-xs">
                  The system will automatically detect the Amazon format and
                  parse your orders for reconciliation.
                </p>
              </div>
              <Button
                onClick={handleGoToTransactions}
                className="gap-2 bg-primary hover:bg-primary/90"
              >
                Go to Transactions
                <ArrowRight className="h-4 w-4" />
              </Button>
              <p className="text-xs text-muted-foreground/80 italic">
                The file is uploaded to the server running this instance and
                parsed there. The orders it contains are saved to your account,
                and the file itself stays with your other uploaded statements.
              </p>
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
              onClick={handleGoToTransactions}
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
