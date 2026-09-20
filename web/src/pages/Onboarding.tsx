import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/auth-store";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ProgressBar } from "@/components/onboarding/ProgressBar";
import { ProfileStep } from "@/components/onboarding/ProfileStep";
import { CompletionScreen } from "@/components/onboarding/CompletionScreen";
import {
  ArrowLeft,
  ArrowRight,
  Upload,
  FileText,
  Shield,
  Loader2,
  CheckCircle,
  AlertTriangle,
} from "lucide-react";

interface OnboardingStatus {
  complete: boolean;
  has_profile: boolean;
  steps?: Array<{ step: string; status: string; completed_at: string | null }>;
  completed_count?: number;
  total_steps?: number;
}

interface OnboardingState {
  state: Record<string, unknown>;
  steps: Array<{ step: string; status: string; completed_at: string | null }>;
}

/**
 * What the two onboarding upload endpoints answer with. Every field is
 * optional, and three different shapes arrive here:
 *
 *  - `/api/transactions/upload-statement` returns the statement import counts;
 *  - `/api/receipts/upload` answers 202 with a queued job (`job_id`, `status`)
 *    — extraction happens on the worker, not in the request;
 *  - the same endpoint answers 200 with the extracted fields when the file was
 *    already extracted once (a duplicate upload).
 *
 * The queued shape is the normal one on a first upload. Reading its absent
 * `vendor`/`total`/`date` as a failed extraction would blame the user's
 * receipt for work that simply has not happened yet.
 */
interface UploadResult {
  // Bank statement import
  imported?: number;
  account?: string | null;
  batch_confidence?: string | null;
  balance_matches?: boolean | null;
  // Receipt extraction
  vendor?: string | null;
  date?: string | null;
  currency?: string | null;
  /** A keyword ("high" / "medium" / "low"), not a number. */
  confidence?: string | null;
  /** Receipt amount when extracting; row count when importing a statement. */
  total?: number | string | null;
  // Queued job (202 from the receipt endpoint)
  job_id?: string;
  status?: string;
  error_code?: string | null;
  error_message?: string | null;
}

/** Job statuses that mean "accepted, not finished" rather than "failed". */
const IN_FLIGHT_JOB_STATUSES = new Set([
  "queued",
  "running",
  "waiting_for_model",
]);

/** Row count from a statement import, tolerating a stringified number. */
function toCount(value: number | string | null | undefined): number {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

const STEPS = [
  { title: "Your Business", key: "profile" },
  { title: "Upload Your First Documents", key: "first_upload" },
];

const ESTIMATED_MINUTES = [2, 3];

/** The code the API uses when the extraction endpoint refused the socket. */
const MODEL_UNREACHABLE_CODE = "LOCAL_MODEL_UNREACHABLE";

/**
 * Why a document has no extracted fields, said in the user's terms.
 *
 * The first branch is the one that matters: when the extraction endpoint is
 * down, nothing was sent and nothing was read, so "try a clearer image" blames
 * the document for an outage — and it is the branch a first-run instance hits
 * most often, before the operator has pointed `LLM_BASE_URL` at anything. The
 * upload itself succeeded and the job is waiting, so say that.
 */
function getExtractionDiagnostic(reason?: string | null, code?: string | null): string {
  const r = (reason ?? "").toLowerCase();
  if (code === MODEL_UNREACHABLE_CODE || r.includes("not reachable") || r.includes("unreachable"))
    return "The extraction model is not reachable; the document is queued and nothing is lost. It will be read once the endpoint answers.";
  if (!reason) return "This document could not be read. Try a clearer image or a different file.";
  if (r.includes("format") || r.includes("unsupported"))
    return "This file format isn't supported. Try a different file.";
  if (r.includes("size") || r.includes("large"))
    return "File exceeds the size limit. Try a smaller file.";
  if (r.includes("timeout"))
    return "Processing took too long. Try a simpler document.";
  if (r.includes("quality") || r.includes("blur"))
    return "Image quality is too low. Try a clearer photo or scan.";
  if (r.includes("financial") || r.includes("not_financial"))
    return "This doesn't appear to be a financial document.";
  return "This document could not be read. Try a clearer image or a different file.";
}

export function Onboarding() {
  const navigate = useNavigate();
  const setOnboarded = useAuthStore((s) => s.setOnboarded);
  const [step, setStep] = useState(0);
  const [showCelebration, setShowCelebration] = useState(false);
  const celebrationRef = useRef(false);
  const [companyName, setCompanyName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [filesUploaded, setFilesUploaded] = useState(false);
  const [isBankUploading, setIsBankUploading] = useState(false);
  const [isReceiptUploading, setIsReceiptUploading] = useState(false);
  const [uploadedBankFiles, setUploadedBankFiles] = useState<string[]>([]);
  const [uploadedReceiptFiles, setUploadedReceiptFiles] = useState<string[]>([]);
  const [extractionResults, setExtractionResults] = useState<Array<{
    vendor: string | null;
    date: string | null;
    total: string | null;
    currency: string | null;
    confidence: string | null;
    extractionFailed?: boolean;
    /** Accepted and waiting on the worker — not a failure. */
    queued?: boolean;
    filename?: string;
    errorReason?: string | null;
    errorCode?: string | null;
  }>>([]);
  const [bankParseResults, setBankParseResults] = useState<Array<{
    filename: string;
    transaction_count: number;
    account_format: string | null;
    batch_confidence: string | null;
    balance_matches: boolean | null;
  }>>([]);
  const bankUploadRef = useRef<HTMLInputElement>(null);
  const receiptUploadRef = useRef<HTMLInputElement>(null);

  const session = useAuthStore((s) => s.session);
  const onboardingChecked = useAuthStore((s) => s.onboardingChecked);

  // Check if onboarding is already complete.
  // Don't fire until auth is fully settled (session exists and onboarding check
  // from auth-store has completed). The api.get() wrapper has 401 retry logic,
  // but it is better not to fire at all until the token is known to be usable.
  const { data: status, isLoading: statusLoading } =
    useQuery<OnboardingStatus>({
      queryKey: ["onboarding", "status"],
      queryFn: () => api.get("/api/onboarding/status"),
      enabled: !!session && onboardingChecked,
    });

  // Fetch saved onboarding state to resume where user left off
  const { data: savedState } = useQuery<OnboardingState>({
    queryKey: ["onboarding", "state"],
    queryFn: () => api.get("/api/onboarding/state"),
    enabled: !statusLoading && !status?.complete,
  });

  // Check for existing uploaded data to restore upload step state
  const { data: existingTxns } = useQuery<{ total: number }>({
    queryKey: ["onboarding", "existing-transactions"],
    queryFn: () => api.get("/api/transactions?page=1&per_page=1"),
    enabled: !statusLoading && !status?.complete && step === 1,
  });

  const { data: existingReceipts } = useQuery<{ total: number }>({
    queryKey: ["onboarding", "existing-receipts"],
    queryFn: () => api.get("/api/receipts?page=1&per_page=1"),
    enabled: !statusLoading && !status?.complete && step === 1,
  });

  // If already onboarded, redirect to dashboard (unless showing celebration)
  useEffect(() => {
    if (status?.complete && !celebrationRef.current) {
      setOnboarded(true);
      navigate("/dashboard", { replace: true });
    }
  }, [status, navigate, setOnboarded]);

  // Resume from saved state
  useEffect(() => {
    if (!savedState || status?.complete) return;

    const completedSteps = savedState.steps.filter(
      (s) => s.status === "COMPLETED" || s.status === "SKIPPED"
    );

    // Find the first incomplete step
    const completedKeys = new Set(completedSteps.map((s) => s.step));
    const nextStepIndex = STEPS.findIndex((s) => !completedKeys.has(s.key));

    if (nextStepIndex >= 0) {
      setStep(nextStepIndex);
    } else if (completedSteps.length >= STEPS.length) {
      // All steps completed but onboarding not marked complete yet
      setStep(STEPS.length - 1);
    }
  }, [savedState, status?.complete]);

  // Restore upload state from existing data (e.g. user refreshed mid-onboarding)
  useEffect(() => {
    if (step !== 1) return;
    const hasTxns = existingTxns && existingTxns.total > 0;
    const hasReceipts = existingReceipts && existingReceipts.total > 0;
    if (hasTxns && uploadedBankFiles.length === 0) {
      setUploadedBankFiles(["Previously uploaded statement"]);
      setFilesUploaded(true);
    }
    if (hasReceipts && uploadedReceiptFiles.length === 0) {
      setUploadedReceiptFiles(["Previously uploaded receipt"]);
      setFilesUploaded(true);
    }
  }, [step, existingTxns, existingReceipts, uploadedBankFiles.length, uploadedReceiptFiles.length]);

  // Save business profile
  const saveProfile = useMutation({
    mutationFn: (data: {
      company_name: string;
      fiscal_year: number;
      base_currency: string;
      province: string;
      industry: string;
      business_summary: string;
      naics_code: string;
    }) => {
      setCompanyName(data.company_name);
      return api.post("/api/onboarding/profile", data);
    },
    onSuccess: () => {
      setError(null);
      // Mark profile step as completed
      api.post("/api/onboarding/state", { step: "profile" });
      setStep(1);
    },
    onError: (err: Error & { status?: number }) => {
      if (err.status === 401 || err.status === 503) {
        // Attempt a final token refresh before giving up
        useAuthStore.getState().forceRefreshToken().then((token) => {
          if (token) {
            // Refresh succeeded -- session is alive. Clear error so user can retry.
            setError(null);
          } else {
            // Refresh truly failed -- session is dead. Redirect to login.
            setError("Your session has expired. Redirecting to sign in...");
            setTimeout(() => navigate("/login"), 2000);
          }
        });
      } else {
        setError(err.message || "Could not save. Check your connection and try again.");
      }
    },
  });

  // Save step state (for bank connect and receipt upload steps)
  const saveStepState = useMutation({
    mutationFn: (stepKey: string) =>
      api.post("/api/onboarding/state", { step: stepKey }),
    onError: (err: Error & { status?: number }) => {
      if (err.status === 401 || err.status === 503) {
        useAuthStore.getState().forceRefreshToken().then((token) => {
          if (token) {
            setError(null);
          } else {
            setError("Your session has expired. Redirecting to sign in...");
            setTimeout(() => navigate("/login"), 2000);
          }
        });
      } else {
        setError(err.message || "Could not save. Check your connection and try again.");
      }
    },
  });

  // Skip a step
  const skipStep = useMutation({
    mutationFn: (stepKey: string) =>
      api.patch(`/api/onboarding/skip/${stepKey}`),
    onError: (err: Error & { status?: number }) => {
      if (err.status === 401 || err.status === 503) {
        useAuthStore.getState().forceRefreshToken().then((token) => {
          if (token) {
            setError(null);
          } else {
            setError("Your session has expired. Redirecting to sign in...");
            setTimeout(() => navigate("/login"), 2000);
          }
        });
      } else {
        setError(err.message || "Could not save. Check your connection and try again.");
      }
    },
  });

  // Complete onboarding
  const completeOnboarding = useMutation({
    mutationFn: () => api.post("/api/onboarding/complete"),
    onSuccess: () => {
      celebrationRef.current = true;
      setShowCelebration(true);
      setOnboarded(true);
    },
    onError: (err: Error & { status?: number }) => {
      if (err.status === 401 || err.status === 503) {
        useAuthStore.getState().forceRefreshToken().then((token) => {
          if (token) {
            setError(null);
          } else {
            setError("Your session has expired. Redirecting to sign in...");
            setTimeout(() => navigate("/login"), 2000);
          }
        });
      } else {
        setError(err.message || "Could not save. Check your connection and try again.");
      }
    },
  });

  const handleFirstUploadComplete = async () => {
    try {
      await saveStepState.mutateAsync("first_upload");
    } catch {
      // saveStepState has its own error handling — proceed to complete
    }
    completeOnboarding.mutate();
  };

  const handleFirstUploadSkip = async () => {
    try {
      await skipStep.mutateAsync("first_upload");
    } catch {
      // skipStep has its own error handling — proceed to complete
    }
    completeOnboarding.mutate();
  };

  const handleFileUpload = useCallback(
    async (files: FileList | null, type: "receipts" | "transactions") => {
      if (!files || files.length === 0) return;
      setError(null);
      const setUploading = type === "transactions" ? setIsBankUploading : setIsReceiptUploading;
      setUploading(true);
      const endpoint =
        type === "receipts"
          ? "/api/receipts/upload"
          : "/api/transactions/upload-statement";

      // Onboarding only: take the first file and replace (not append)
      const file = files[0];
      const formData = new FormData();
      formData.append("file", file);
      try {
        const result = await api.upload<UploadResult>(endpoint, formData);
        // Capture parse results for bank statements (replace, not append)
        if (type === "transactions" && result) {
          setBankParseResults([{
            filename: file.name,
            transaction_count: result.imported ?? toCount(result.total),
            account_format: result.account ?? null,
            batch_confidence: result.batch_confidence ?? null,
            balance_matches: result.balance_matches ?? null,
          }]);
        }
        // Capture extraction results for receipts (replace, not append).
        //
        // Three answers are possible and they mean different things: fields
        // (already extracted), a queued job (accepted, the worker has it), or
        // neither. Only the third is a failure.
        if (type === "receipts") {
          const queued =
            !!result?.job_id ||
            (typeof result?.status === "string" && IN_FLIGHT_JOB_STATUSES.has(result.status));
          if (result?.vendor || result?.total || result?.date) {
            setExtractionResults([{
              vendor: result.vendor ?? null,
              date: result.date ?? null,
              total: result.total != null ? String(result.total) : null,
              currency: result.currency ?? null,
              confidence: result.confidence ?? null,
              extractionFailed: false,
              filename: file.name,
            }]);
          } else if (queued) {
            setExtractionResults([{
              vendor: null,
              date: null,
              total: null,
              currency: null,
              confidence: null,
              queued: true,
              filename: file.name,
              errorCode: result?.error_code ?? null,
              errorReason: result?.error_message ?? null,
            }]);
          } else {
            setExtractionResults([{
              vendor: null,
              date: null,
              total: null,
              currency: null,
              confidence: null,
              extractionFailed: true,
              filename: file.name,
              errorReason: null,
            }]);
          }
        }
        // Replace file list (not append) — onboarding allows only 1 of each
        if (type === "transactions") {
          setUploadedBankFiles([file.name]);
        } else {
          setUploadedReceiptFiles([file.name]);
        }
        setFilesUploaded(true);
      } catch (err: unknown) {
        const error = err as { message?: string; detail?: string; status?: number; code?: string };
        const errorDetail = error.detail || error.message || `Failed to upload ${file.name}`;
        setError(errorDetail);
        if (type === "receipts") {
          setExtractionResults([{
            vendor: null,
            date: null,
            total: null,
            currency: null,
            confidence: null,
            extractionFailed: true,
            filename: file.name,
            errorReason: errorDetail,
            errorCode: error.code ?? null,
          }]);
        }
      }
      setUploading(false);
    },
    []
  );

  const handleGoToDashboard = () => {
    setOnboarded(true);
    navigate("/dashboard", { replace: true });
  };

  // Estimate remaining minutes based on current step
  const estimatedMinutes = ESTIMATED_MINUTES.slice(step).reduce(
    (sum, m) => sum + m,
    0
  );

  if (statusLoading) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center px-4 bg-background">
        <Card className="w-full max-w-lg shadow-warm rounded-xl">
          <CardContent className="space-y-6 p-8">
            <Skeleton className="mx-auto h-12 w-12 rounded-xl" />
            <Skeleton className="mx-auto h-6 w-48" />
            <Skeleton className="mx-auto h-4 w-64" />
            <div className="space-y-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  // Completion screen
  if (showCelebration) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center px-4 bg-background">
        <Card className="w-full max-w-lg shadow-warm rounded-xl">
          <CardContent className="p-8">
            <CompletionScreen
              companyName={companyName || "there"}
              onGoToDashboard={handleGoToDashboard}
              hasFiles={uploadedBankFiles.length > 0 && uploadedReceiptFiles.length > 0}
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-4 bg-background">
      {/* Progress bar */}
      <div className="mb-8">
        <ProgressBar
          currentStep={step}
          totalSteps={STEPS.length}
          estimatedMinutes={estimatedMinutes}
        />
      </div>

      {/* Error banner */}
      {error && (
        <div className="mb-4 w-full max-w-lg rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* Step content */}
      <Card className="w-full max-w-lg shadow-warm rounded-xl">
        <CardContent className="p-8">
          {step === 0 && (
            <ProfileStep
              onNext={(data) => saveProfile.mutate(data)}
              isSubmitting={saveProfile.isPending}
              defaultValues={savedState?.state as {
                company_name?: string;
                fiscal_year?: number;
                base_currency?: string;
                province?: string;
                industry?: string;
                business_summary?: string;
                naics_code?: string;
              } | undefined}
            />
          )}
          {step === 1 && (
            <div className="space-y-6">
              {/* Header */}
              <div className="mx-auto mb-2 flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
                <Upload className="h-7 w-7 text-primary" />
              </div>
              <div className="text-center">
                <h2 className="text-2xl font-bold tracking-tight">Upload Your First Documents</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Start with a bank or credit-card statement &mdash; a CSV or PDF
                  in one of the formats listed in docs/data-schema.md. You can
                  always add more later.
                </p>
                <p className="mt-2 text-xs text-muted-foreground/70">
                  Extraction uses the LLM endpoint the operator configured. After
                  uploading, transactions are matched to receipts, and you review
                  every match before anything is finalized.
                </p>
              </div>

              {/* Primary: Bank statement upload */}
              <div>
                <p className="text-sm font-medium mb-2 flex items-center gap-2">
                  <FileText className="h-4 w-4 text-primary" />
                  Bank Statement
                </p>
                <div
                  className={`rounded-xl border-2 border-dashed p-6 text-center cursor-pointer transition-colors ${
                    isBankUploading
                      ? "border-primary/40 bg-primary/5"
                      : uploadedBankFiles.length > 0
                        ? "border-primary/30 bg-primary/5"
                        : "border-muted-foreground/25 bg-muted/30 hover:border-primary/40"
                  }`}
                  tabIndex={0}
                  role="button"
                  aria-label="Upload bank statement"
                  onClick={() => !isBankUploading && bankUploadRef.current?.click()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      if (!isBankUploading) bankUploadRef.current?.click();
                    }
                  }}
                  onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
                  onDrop={(e) => {
                    e.preventDefault();
                    if (!isBankUploading) handleFileUpload(e.dataTransfer.files, "transactions");
                  }}
                >
                  {isBankUploading ? (
                    <div className="flex flex-col items-center gap-2">
                      <Loader2 className="h-5 w-5 text-primary animate-spin" />
                      <p className="text-sm text-primary font-medium">Uploading and processing...</p>
                    </div>
                  ) : uploadedBankFiles.length > 0 ? (
                    <div className="space-y-2">
                      {uploadedBankFiles.map((name) => (
                        <div key={name} className="flex items-center justify-center gap-2 text-sm text-primary">
                          <CheckCircle className="h-4 w-4" />
                          <span className="font-medium">{name}</span>
                        </div>
                      ))}
                      <p className="text-xs text-muted-foreground mt-1">
                        Click to replace
                      </p>
                    </div>
                  ) : (
                    <>
                      <p className="text-sm text-muted-foreground">
                        Drop your bank statement here, or{" "}
                        <span className="text-primary font-medium">browse</span>
                      </p>
                      <p className="text-xs text-muted-foreground/60 mt-1">
                        CSV, PDF, XLSX, or XLS from any bank or financial institution
                      </p>
                    </>
                  )}
                </div>
                <input
                  ref={bankUploadRef}
                  type="file"
                  accept=".csv,.pdf,.xlsx,.xls"
                  className="hidden"
                  onChange={(e) => {
                    handleFileUpload(e.target.files, "transactions");
                    if (e.target) e.target.value = "";
                  }}
                />
              </div>

              {/* Bank statement parse results */}
              {bankParseResults.length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Parsed from your statements
                  </p>
                  {bankParseResults.map((result, i) => (
                    <div
                      key={i}
                      className="rounded-lg border border-border/60 bg-card p-3 flex items-center justify-between text-sm"
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <CheckCircle className="h-4 w-4 text-primary shrink-0" />
                        <span className="font-medium truncate">{result.filename}</span>
                      </div>
                      <div className="flex items-center gap-3 shrink-0 text-xs text-muted-foreground">
                        {result.transaction_count > 0 && (
                          <span className="font-mono font-semibold text-foreground">
                            {result.transaction_count} transaction{result.transaction_count !== 1 ? "s" : ""}
                          </span>
                        )}
                        {result.account_format && (
                          <span>{result.account_format}</span>
                        )}
                        {result.balance_matches === true && (
                          <span className="inline-flex items-center rounded-full bg-success/15 text-success px-1.5 py-0.5 text-[10px] font-semibold leading-none">
                            balanced
                          </span>
                        )}
                        {result.balance_matches === false && (
                          <span className="inline-flex items-center rounded-full bg-amber-100 text-amber-700 px-1.5 py-0.5 text-[10px] font-semibold leading-none">
                            review needed
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Primary: Receipt upload with inline extraction */}
              <div>
                <p className="text-sm font-medium mb-2 flex items-center gap-2">
                  <FileText className="h-4 w-4 text-primary" />
                  Receipt or Invoice
                </p>
                <div
                  className={`rounded-xl border-2 border-dashed p-6 text-center cursor-pointer transition-colors ${
                    isReceiptUploading
                      ? "border-primary/40 bg-primary/5"
                      : uploadedReceiptFiles.length > 0
                        ? "border-primary/30 bg-primary/5"
                        : "border-muted-foreground/25 bg-muted/30 hover:border-primary/40"
                  }`}
                  tabIndex={0}
                  role="button"
                  aria-label="Upload receipt or invoice"
                  onClick={() => !isReceiptUploading && receiptUploadRef.current?.click()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      if (!isReceiptUploading) receiptUploadRef.current?.click();
                    }
                  }}
                  onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
                  onDrop={(e) => {
                    e.preventDefault();
                    if (!isReceiptUploading) handleFileUpload(e.dataTransfer.files, "receipts");
                  }}
                >
                  {isReceiptUploading ? (
                    <div className="flex flex-col items-center gap-2">
                      <Loader2 className="h-5 w-5 text-primary animate-spin" />
                      <p className="text-sm text-primary font-medium">Uploading…</p>
                    </div>
                  ) : uploadedReceiptFiles.length > 0 ? (
                    <div className="space-y-2">
                      {uploadedReceiptFiles.map((name) => (
                        <div key={name} className="flex items-center justify-center gap-2 text-sm text-primary">
                          <CheckCircle className="h-4 w-4" />
                          <span className="font-medium">{name}</span>
                        </div>
                      ))}
                      <p className="text-xs text-muted-foreground mt-1">
                        Click to replace
                      </p>
                    </div>
                  ) : (
                    <>
                      <p className="text-sm text-muted-foreground">
                        Drop a receipt or invoice here, or{" "}
                        <span className="text-primary font-medium">browse</span>
                      </p>
                      <p className="text-xs text-muted-foreground/60 mt-1">
                        PDF, images, spreadsheets, or documents — each one is queued and read by the extraction model, and the details appear here when it answers
                      </p>
                    </>
                  )}
                </div>
                <input
                  ref={receiptUploadRef}
                  type="file"
                  accept=".pdf,.jpg,.jpeg,.jfif,.png,.webp,.gif,.bmp,.tiff,.tif,.xlsx,.xls,.csv,.docx,.doc"
                  className="hidden"
                  onChange={(e) => {
                    handleFileUpload(e.target.files, "receipts");
                    if (e.target) e.target.value = "";
                  }}
                />
              </div>

              {filesUploaded && (
                <div className="rounded-lg border border-primary/20 bg-primary/5 p-3 text-center">
                  <p className="text-sm text-primary font-medium">Files uploaded successfully</p>
                </div>
              )}

              {/* Extraction results preview */}
              {extractionResults.length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Your receipts
                  </p>
                  {extractionResults.map((result, i) => (
                    result.queued ? (
                      /* Accepted and waiting on the worker. Nothing has gone
                         wrong, so this row offers no Retry and blames nothing. */
                      <div
                        key={i}
                        className="rounded-lg border border-primary/20 bg-primary/[0.03] p-3 flex items-center gap-3 text-sm"
                      >
                        <Loader2 className="h-4 w-4 text-primary shrink-0 animate-spin" />
                        <div className="min-w-0">
                          <span className="font-medium text-foreground truncate block">
                            Queued for extraction
                          </span>
                          <span className="text-xs text-muted-foreground truncate block">{result.filename}</span>
                          <span className="text-[11px] text-muted-foreground/80 block mt-0.5">
                            {result.errorCode || result.errorReason
                              ? getExtractionDiagnostic(result.errorReason, result.errorCode)
                              : "The details appear on the Transactions page once the extraction model has read it."}
                          </span>
                        </div>
                      </div>
                    ) : result.extractionFailed ? (
                      <div
                        key={i}
                        className="rounded-lg border border-amber-300/60 bg-amber-50 p-3 flex items-center justify-between text-sm"
                      >
                        <div className="flex items-center gap-3 min-w-0">
                          <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0" />
                          <div className="min-w-0">
                            <span className="font-medium text-amber-800 truncate block">
                              {result.errorCode === MODEL_UNREACHABLE_CODE
                                ? "Waiting for the extraction model"
                                : "Could not extract details"}
                            </span>
                            <span className="text-xs text-amber-600 truncate block">{result.filename}</span>
                            <span className="text-[11px] text-amber-600/80 block mt-0.5">
                              {getExtractionDiagnostic(result.errorReason, result.errorCode)}
                            </span>
                          </div>
                        </div>
                        <Button
                          variant="outline"
                          size="sm"
                          className="text-xs border-amber-300 text-amber-700 hover:bg-amber-100 shrink-0 h-7"
                          onClick={() => {
                            setExtractionResults((prev) => prev.filter((_, idx) => idx !== i));
                            receiptUploadRef.current?.click();
                          }}
                        >
                          Retry
                        </Button>
                      </div>
                    ) : (
                      <div
                        key={i}
                        className="rounded-lg border border-border/60 bg-card p-3 flex items-center justify-between text-sm"
                      >
                        <div className="flex items-center gap-3 min-w-0">
                          <span className="font-medium truncate">{result.vendor ?? "Unknown"}</span>
                          {result.date && <span className="text-muted-foreground text-xs">{result.date}</span>}
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          {result.total && (
                            <span className="font-mono font-semibold">
                              ${Number(result.total).toFixed(2)} {result.currency ?? "CAD"}
                            </span>
                          )}
                          {result.confidence && (
                            <span className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold leading-none ${
                              result.confidence === "high"
                                ? "bg-success/15 text-success"
                                : result.confidence === "medium"
                                ? "bg-amber-100 text-amber-700"
                                : "bg-destructive/10 text-destructive"
                            }`}>
                              {result.confidence}
                            </span>
                          )}
                        </div>
                      </div>
                    )
                  ))}
                </div>
              )}

              {/* What happens next — after both types uploaded */}
              {uploadedBankFiles.length > 0 && uploadedReceiptFiles.length > 0 && (
                <div className="rounded-xl border border-primary/20 bg-primary/[0.03] p-4 text-center space-y-2">
                  <div className="flex items-center justify-center gap-2">
                    <CheckCircle className="h-4 w-4 text-primary" />
                    <p className="text-sm font-semibold text-foreground">
                      Ready to match
                    </p>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    After setup, this instance compares your {uploadedBankFiles.length} statement{uploadedBankFiles.length > 1 ? "s" : ""} against your {uploadedReceiptFiles.length} receipt{uploadedReceiptFiles.length > 1 ? "s" : ""} to find matches.
                    You&apos;ll review and confirm each match on the Review page.
                  </p>
                </div>
              )}

              {/* Privacy framing */}
              <div className="flex items-start gap-2 rounded-lg bg-muted/30 p-3">
                <Shield className="h-4 w-4 text-primary shrink-0 mt-0.5" />
                <p className="text-xs text-muted-foreground">
                  Your data stays under your control. No bank passwords shared with third parties.
                  Documents are stored on the machine this instance runs on, not a third-party cloud.
                </p>
              </div>

              {/* Navigation */}
              <div className="flex items-center justify-between pt-2">
                <Button
                  variant="ghost"
                  onClick={() => setStep(0)}
                  className="gap-2 text-muted-foreground hover:text-foreground"
                >
                  <ArrowLeft className="h-4 w-4" />
                  Back
                </Button>
                <div className="flex items-center gap-2">
                  <Button
                    variant="ghost"
                    onClick={handleFirstUploadSkip}
                    disabled={saveStepState.isPending || completeOnboarding.isPending}
                    className="text-muted-foreground"
                  >
                    Skip for now
                  </Button>
                  <Button
                    onClick={handleFirstUploadComplete}
                    disabled={!filesUploaded || isBankUploading || isReceiptUploading || saveStepState.isPending || completeOnboarding.isPending}
                    className="gap-2 bg-primary hover:bg-primary/90"
                  >
                    Complete Setup
                    <ArrowRight className="h-4 w-4" />
                  </Button>
                </div>
              </div>
              {!filesUploaded && (
                <p className="text-xs text-muted-foreground text-right">
                  Upload at least one file to continue
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Footer text */}
      <p className="mt-6 text-center text-xs text-muted-foreground">
        You can change these settings anytime from the Settings page.
      </p>

    </div>
  );
}
