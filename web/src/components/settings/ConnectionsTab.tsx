import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { ConnectionCard } from "./ConnectionCard";
import { GmailConsentStep } from "./GmailConsentStep";
import { GmailScanPanel } from "./GmailScanPanel";
import { ScanHistoryTable } from "./ScanHistoryTable";
import { useGmailOAuth } from "./GmailOAuthPopup";
import { WiseTokenWalkthrough } from "@/components/WiseTokenWalkthrough";
import { useNavigate } from "react-router-dom";
import { AmazonImportWalkthrough } from "@/components/AmazonImportWalkthrough";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import {
  ArrowLeftRight,
  Wallet,
  ShoppingCart,
  Loader2,
  XCircle,
  AlertCircle,
  Mail,
  Shield,
  Eye,
  Zap,
} from "lucide-react";

interface SourceConfig {
  enabled: boolean;
  connected: boolean;
  config_json?: Record<string, unknown>;
}

interface OnboardingConfig {
  sources?: {
    gmail?: SourceConfig;
    wise?: SourceConfig;
    paypal?: SourceConfig;
    amazon?: SourceConfig;
  };
}

interface ConnectionsTabProps {
  config: OnboardingConfig | null;
  isLoading: boolean;
}

interface ConnectionHealthInfo {
  source: string;
  enabled: boolean;
  connected: boolean;
  account_email?: string | null;
  status: string;
  last_checked: string | null;
  last_success: string | null;
  error_message: string | null;
}

interface ConnectionsHealthResponse {
  connections: Record<string, ConnectionHealthInfo>;
}

export function ConnectionsTab({ config, isLoading }: ConnectionsTabProps) {
  const queryClient = useQueryClient();

  // ── Wise state ──────────────────────────────────────────────────
  const [wiseToken, setWiseToken] = useState("");
  const [wiseConnectError, setWiseConnectError] = useState("");
  const [wiseWalkthroughOpen, setWiseWalkthroughOpen] = useState(false);
  const [wiseTokenTouched, setWiseTokenTouched] = useState(false);
  const [wiseJustConnected, setWiseJustConnected] = useState(false);
  const navigate = useNavigate();

  // ── Gmail state ─────────────────────────────────────────────────
  const [gmailConsentOpen, setGmailConsentOpen] = useState(false);
  const [gmailJustConnected, setGmailJustConnected] = useState(false);
  const [gmailError, setGmailError] = useState<string | null>(null);

  // PayPal has no connection to hold: the card below is a guide to exporting a
  // CSV and uploading it on the Transactions page, so there is nothing to
  // connect, nothing to store and nothing to disconnect.

  // ── Amazon state ───────────────────────────────────────────────
  const [amazonWalkthroughOpen, setAmazonWalkthroughOpen] = useState(false);
  const [amazonJustConnected, setAmazonJustConnected] = useState(false);

  // ── Disconnect confirmation ───────────────────────────────
  const [disconnectTarget, setDisconnectTarget] = useState<string | null>(null);

  const { data: healthData } = useQuery<ConnectionsHealthResponse>({
    queryKey: ["connections", "health"],
    queryFn: () => api.get("/api/connections/health"),
    enabled: !isLoading,
  });

  const connectSourceMutation = useMutation({
    mutationFn: ({ type, data }: { type: string; data: Record<string, unknown> }) =>
      api.post(`/api/onboarding/source/${type}`, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
      queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
    },
    onError: () => {
      toast.error("Couldn't update the connection — please try again.");
    },
  });

  const reconnectMutation = useMutation({
    mutationFn: (source: string) =>
      api.post<{ action: string; auth_url?: string; status?: string }>(
        `/api/connections/${source}/reconnect`
      ),
    onSuccess: (data) => {
      if (data.action === "oauth" && data.auth_url) {
        window.location.href = data.auth_url;
      }
      queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
      queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
    },
    onError: () => {
      toast.error("Couldn't reconnect — please try again.");
    },
  });

  const gmailDisconnectMutation = useMutation({
    mutationFn: () => api.delete("/api/onboarding/gmail/disconnect"),
    onSuccess: () => {
      setGmailJustConnected(false);
      queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
      queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
      queryClient.invalidateQueries({ queryKey: ["scan-status"] });
      queryClient.invalidateQueries({ queryKey: ["scan-history"] });
    },
    onError: () => {
      toast.error("Couldn't disconnect Gmail — please try again.");
    },
  });

  // Gmail OAuth popup hook
  const { openPopup: openGmailOAuth, isLoading: gmailOAuthLoading } =
    useGmailOAuth({
      onSuccess: () => {
        setGmailJustConnected(true);
        setGmailError(null);
        setGmailConsentOpen(false);
        queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
        queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
      },
      onError: (error) => {
        setGmailError(error === "USER_DENIED" ? "Connection cancelled" : error);
        setGmailConsentOpen(false);
      },
    });

  function handleReconnect(source: string) {
    reconnectMutation.mutate(source);
  }

  const health = healthData?.connections ?? {};

  const connectWiseMutation = useMutation({
    mutationFn: () =>
      api.post("/api/onboarding/wise/test", { token: wiseToken }),
    onSuccess: () => {
      setWiseJustConnected(true);
      setWiseConnectError("");
      queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
      queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
    },
    onError: (err: unknown) => {
      const detail = (err as { detail?: string })?.detail;
      setWiseConnectError(detail || "Failed to connect — check your token and try again");
    },
  });

  const disconnectAmazonMutation = useMutation({
    mutationFn: () => api.delete("/api/onboarding/amazon/disconnect"),
    onSuccess: () => {
      setAmazonJustConnected(false);
      queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
      queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
    },
    onError: () => {
      toast.error("Couldn't disconnect Amazon — please try again.");
    },
  });

  const disconnectWiseMutation = useMutation({
    mutationFn: () => api.delete("/api/onboarding/wise/disconnect"),
    onSuccess: () => {
      setWiseJustConnected(false);
      queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
      queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: () => {
      toast.error("Couldn't disconnect Wise — please try again.");
    },
  });

  function handleDisconnect(type: string) {
    if (type === "amazon") {
      disconnectAmazonMutation.mutate();
      return;
    }
    if (type === "wise") {
      disconnectWiseMutation.mutate();
      return;
    }
    if (type === "gmail") {
      gmailDisconnectMutation.mutate();
      return;
    }
    connectSourceMutation.mutate({
      type,
      data: { enabled: false, config_json: {} },
    });
  }

  if (isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Card key={i} className="shadow-warm rounded-xl">
            <CardHeader>
              <div className="flex items-start gap-4">
                <Skeleton className="h-10 w-10 rounded-xl" />
                <div className="space-y-2 flex-1">
                  <Skeleton className="h-5 w-32" />
                  <Skeleton className="h-4 w-48" />
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <Skeleton className="h-9 w-24" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  const sources = config?.sources ?? {};
  const wise = sources.wise ?? { enabled: true, connected: false };
  const wiseConnected = wise.connected || wiseJustConnected;
  const gmail = sources.gmail ?? { enabled: true, connected: false };
  const gmailConnected = gmail.connected || gmailJustConnected;
  const gmailEmail = health.gmail?.account_email;
  const amazon = sources.amazon ?? { enabled: false, connected: false };
  const amazonConnected = amazon.connected || amazonJustConnected;

  return (
    <>
      {/* What this page can honestly say about a connection. Nothing here may
          describe what happens at the other end of the extraction endpoint:
          retention and training are that endpoint's policy, not this
          software's, so the line points at Settings → Security, which reads the
          configured endpoint off the server. */}
      <div className="flex flex-wrap items-center gap-4 mb-4 rounded-xl border border-border/40 bg-muted/20 px-4 py-3">
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Shield className="h-3.5 w-3.5 text-primary" />
          <span>Credentials encrypted, never shown again</span>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Eye className="h-3.5 w-3.5 text-primary" />
          <span>Read-only access</span>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Zap className="h-3.5 w-3.5 text-primary" />
          <span>
            Documents are read by the extraction endpoint this instance is
            configured with — see Security
          </span>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Gmail — read-only OAuth, keyword search, on-server relevance
            scoring. The scoring is deterministic (text extraction plus rules);
            the documents it keeps are read by the extraction endpoint later,
            like any other upload. */}
        <ConnectionCard
          title="Gmail"
          description="Fetch receipts and invoices from your inbox. Read-only access, vendor-keyword search, on-server relevance scoring; only matching documents are saved."
          icon={<Mail className="h-5 w-5" />}
          connected={gmailConnected}
          enabled={gmail.enabled}
          loading={gmailDisconnectMutation.isPending || gmailOAuthLoading}
          healthStatus={health.gmail?.status as "HEALTHY" | "DEGRADED" | "ERROR" | "DISCONNECTED" | "UNKNOWN" | undefined}
          lastSync={health.gmail?.last_success}
          onConnect={() => setGmailConsentOpen(true)}
          onDisconnect={() => setDisconnectTarget("gmail")}
          onReconnect={() => setGmailConsentOpen(true)}
        >
          {gmailConnected && (
            <div className="space-y-4">
              {gmailEmail && (
                <p className="text-xs text-muted-foreground">
                  Connected as <span className="font-medium text-foreground">{gmailEmail}</span>
                </p>
              )}
              <GmailScanPanel />
              <ScanHistoryTable />
            </div>
          )}
          {gmailError && (
            <p className="mt-2 flex items-center text-xs text-rose-600">
              <XCircle className="h-3 w-3 mr-1 shrink-0" />
              {gmailError}
            </p>
          )}
        </ConnectionCard>

        {/* Wise */}
        <ConnectionCard
          title="Wise"
          description="Sync multi-currency transactions and transfer documents."
          icon={<ArrowLeftRight className="h-5 w-5" />}
          connected={wiseConnected}
          enabled={wise.enabled}
          loading={disconnectWiseMutation.isPending}
          healthStatus={health.wise?.status as "HEALTHY" | "DEGRADED" | "ERROR" | "DISCONNECTED" | "UNKNOWN" | undefined}
          lastSync={health.wise?.last_success}
          onDisconnect={() => setDisconnectTarget("wise")}
          onReconnect={() => handleReconnect("wise")}
        >
          {!wiseConnected && (
            <div className="space-y-3">
              <div className="space-y-2">
                <Label htmlFor="wise-token">
                  Access Token <span className="text-destructive">*</span>
                </Label>
                <Input
                  id="wise-token"
                  type="password"
                  placeholder="Enter your Wise access token"
                  value={wiseToken}
                  onChange={(e) => {
                    setWiseToken(e.target.value);
                    setWiseConnectError("");
                    if (wiseTokenTouched && e.target.value.trim()) {
                      setWiseTokenTouched(false);
                    }
                  }}
                  onBlur={() => {
                    if (!wiseToken.trim()) setWiseTokenTouched(true);
                  }}
                  required
                  aria-invalid={wiseTokenTouched && !wiseToken.trim()}
                  className={`font-mono focus-visible:ring-primary ${
                    wiseTokenTouched && !wiseToken.trim() ? "border-destructive focus-visible:ring-destructive" : ""
                  }`}
                />
                {wiseTokenTouched && !wiseToken.trim() && (
                  <p className="flex items-center gap-1 text-sm text-destructive">
                    <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                    Access token is required.
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <Button
                  size="sm"
                  onClick={() => connectWiseMutation.mutate()}
                  disabled={!wiseToken.trim() || connectWiseMutation.isPending}
                  className="bg-primary hover:bg-primary/90"
                >
                  {connectWiseMutation.isPending ? (
                    <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                  ) : null}
                  Connect
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setWiseWalkthroughOpen(true)}
                  className="text-muted-foreground"
                >
                  Need help?
                </Button>
                {wiseConnectError && (
                  <span className="flex items-center text-xs text-rose-600">
                    <XCircle className="h-3 w-3 mr-1 shrink-0" />
                    {wiseConnectError}
                  </span>
                )}
              </div>
            </div>
          )}
        </ConnectionCard>

        {/* PayPal — CSV upload guide */}
        <Card className="shadow-warm rounded-xl">
          <CardHeader className="flex flex-row items-start gap-4 space-y-0">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border bg-primary/5 text-primary">
              <Wallet className="h-5 w-5" />
            </div>
            <div className="flex-1 space-y-1">
              <div className="flex items-center gap-2">
                <h3 className="text-base font-semibold leading-none tracking-tight">PayPal</h3>
              </div>
              <p className="text-sm text-muted-foreground">
                Import PayPal transactions by uploading a CSV export.
              </p>
            </div>
          </CardHeader>
          <CardContent>
            <ol className="text-sm text-muted-foreground space-y-2 list-decimal list-inside mb-4">
              <li>Log in to <strong>paypal.com</strong></li>
              <li>Go to <strong>Activity</strong> &rarr; <strong>All Transactions</strong></li>
              <li>Click <strong>Download</strong> and select your date range</li>
              <li>Choose <strong>CSV</strong> format and download</li>
              <li>Upload the file using <strong>Upload Statements</strong> on the Transactions page</li>
            </ol>
            <Button
              size="sm"
              className="bg-primary hover:bg-primary/90"
              onClick={() => navigate("/transactions")}
            >
              Go to Transactions
            </Button>
          </CardContent>
        </Card>

        {/* Amazon */}
        <ConnectionCard
          title="Amazon"
          description="Import Amazon orders from a CSV export you download yourself. Nothing here signs in to Amazon."
          icon={<ShoppingCart className="h-5 w-5" />}
          connected={amazonConnected}
          enabled={true}
          loading={disconnectAmazonMutation.isPending}
          healthStatus={health.amazon?.status as "HEALTHY" | "DEGRADED" | "ERROR" | "DISCONNECTED" | "UNKNOWN" | undefined}
          lastSync={health.amazon?.last_success}
          onConnect={() => setAmazonWalkthroughOpen(true)}
          onDisconnect={() => setDisconnectTarget("amazon")}
        />
      </div>

      {/* Gmail consent dialog */}
      <GmailConsentStep
        open={gmailConsentOpen}
        onOpenChange={setGmailConsentOpen}
        onContinue={() => {
          setGmailConsentOpen(false);
          openGmailOAuth();
        }}
      />

      {/* Wise token walkthrough dialog */}
      <WiseTokenWalkthrough
        open={wiseWalkthroughOpen}
        onOpenChange={setWiseWalkthroughOpen}
        onSuccess={() => {
          setWiseJustConnected(true);
          queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
          queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
        }}
      />

      {/* Amazon import walkthrough dialog */}
      <AmazonImportWalkthrough
        open={amazonWalkthroughOpen}
        onOpenChange={setAmazonWalkthroughOpen}
        onSuccess={() => {
          setAmazonJustConnected(true);
          queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
          queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
          queryClient.invalidateQueries({ queryKey: ["transactions"] });
        }}
      />

      {/* Disconnect confirmation dialog */}
      <ConfirmDialog
        open={disconnectTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDisconnectTarget(null);
        }}
        title={`Disconnect ${disconnectTarget ? disconnectTarget.charAt(0).toUpperCase() + disconnectTarget.slice(1) : ""}?`}
        description="This will remove the connection. You can reconnect at any time, but pending sync data may be lost."
        confirmLabel="Disconnect"
        variant="destructive"
        onConfirm={() => {
          if (disconnectTarget) {
            if (disconnectTarget === "wise") setWiseJustConnected(false);
            if (disconnectTarget === "amazon") setAmazonJustConnected(false);
            if (disconnectTarget === "gmail") setGmailJustConnected(false);
            handleDisconnect(disconnectTarget);
            setDisconnectTarget(null);
          }
        }}
      />
    </>
  );
}
