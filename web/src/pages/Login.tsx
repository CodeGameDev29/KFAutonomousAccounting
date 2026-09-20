import { useState, useEffect } from "react";
import { useNavigate, useLocation, useSearchParams } from "react-router-dom";
import { Shield, CheckCircle, EyeOff, Lock, AlertCircle, Check, X } from "lucide-react";
import { Logo } from "@/components/Logo";
import { authClient } from "@/lib/auth-client";
import { useSignupEnabled } from "@/hooks/use-system-info";
import { authClient as _authClient } from "@/lib/auth-client";
import { useAuth } from "@/hooks/use-auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { getFriendlyAuthError } from "@/lib/auth-errors";
import {
  getPasswordChecks,
  getPasswordScore,
  getPasswordStrengthLabel,
  getPasswordStrengthColor,
  validateNewPassword,
  validateSignInPassword,
  NEW_PASSWORD_HINT,
} from "@/lib/password-validation";

type Mode = "signin" | "signup" | "forgot" | "reset";

interface FieldErrors {
  email?: string;
  password?: string;
  newPassword?: string;
}

function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

/** Left brand panel — indigo gradient */
function BrandPanel() {
  return (
    <div className="hidden lg:flex lg:w-1/2 relative overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-br from-primary via-primary-hover to-[#312E81]" />
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_bottom_left,_rgba(255,255,255,0.08)_0%,_transparent_60%)]" />

      <div className="relative flex flex-col justify-center px-16 py-12 text-white">
        <div className="flex items-center gap-3 mb-12">
          <Logo size="lg" className="rounded-xl" />
          <span className="text-xl font-semibold">Autonomous Accounting</span>
        </div>

        <h2 className="text-4xl font-bold leading-snug mb-6">
          Bookkeeping that runs on the machine you put it on.
        </h2>

        <p className="text-white/70 text-base leading-relaxed mb-10 max-w-md">
          Upload bank statements and receipts. The engine reads them, matches
          each transaction to the document that proves it, categorises what it
          can against the Canadian ISED/GIFI taxonomy, and exports a month you
          can hand to an accountant.
        </p>

        <div className="space-y-4">
          {[
            "Receipts, invoices and bank statements, read by a model you configure",
            "Every transaction matched to its receipt, and every match reviewable",
            "Self-hosted: this instance runs wherever its operator put it",
          ].map((item) => (
            <div key={item} className="flex items-center gap-3 text-white/80 text-sm">
              <CheckCircle className="h-4 w-4 text-white/60 flex-shrink-0" />
              {item}
            </div>
          ))}
        </div>

        <div className="mt-auto pt-16 flex items-center gap-2 text-white/40 text-xs">
          <Shield className="h-3.5 w-3.5" />
          Your data is stored on the machine this instance runs on.
        </div>
      </div>
    </div>
  );
}

/** What this instance can actually say about itself, below the form cards.
 *  Nothing here may be a promise the software cannot keep: how the instance is
 *  exposed, and where its documents are sent, are the operator's choices. */
function TrustBar() {
  return (
    <div className="mt-6 flex flex-col sm:flex-row items-center justify-center gap-4 sm:gap-6 text-xs text-[#9CA3AF]">
      <div className="flex items-center gap-1.5">
        <Shield className="h-3.5 w-3.5 flex-shrink-0" />
        <span>Your data stays on the machine this instance runs on</span>
      </div>
      <div className="flex items-center gap-1.5">
        <EyeOff className="h-3.5 w-3.5 flex-shrink-0" />
        <span>No analytics or telemetry in this application</span>
      </div>
      <div className="flex items-center gap-1.5">
        <Lock className="h-3.5 w-3.5 flex-shrink-0" />
        <span>Downloads use short-lived signed links</span>
      </div>
    </div>
  );
}

/** Wraps form cards with the split layout */
function PageWrapper({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <BrandPanel />
      <div className="flex flex-1 items-center justify-center px-6 py-12 bg-background">
        <div className="w-full max-w-sm">
          {children}
          <TrustBar />
        </div>
      </div>
    </div>
  );
}

export function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const { signInWithEmail, signUp, loading, isAuthenticated, initialized } = useAuth();
  // true = open, false = closed, undefined = the server has not answered yet.
  const signupEnabled = useSignupEnabled();

  const [searchParams] = useSearchParams();

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [signupSuccess, setSignupSuccess] = useState(false);
  const [confirmUrl, setConfirmUrl] = useState<string | null>(null);
  const [resetEmailSent, setResetEmailSent] = useState(false);
  const [passwordUpdated, setPasswordUpdated] = useState(false);
  const [resendCooldown, setResendCooldown] = useState(0);
  const [resendStatus, setResendStatus] = useState<"idle" | "sent" | "error">("idle");
  const [submitCooldown, setSubmitCooldown] = useState(0);
  // True when the recovery link carries no usable token. The reset form
  // renders a dedicated "link expired" view rather than letting the user
  // submit into a failure they cannot act on.
  const [resetLinkInvalid, setResetLinkInvalid] = useState(false);
  // Only offered when the server actually has a Google OAuth client
  // configured, so the button is never a dead end.
  const [googleEnabled, setGoogleEnabled] = useState(false);

  const from = (location.state as { from?: { pathname: string } })?.from
    ?.pathname ?? "/dashboard";

  // Redirect authenticated users away from login page
  useEffect(() => {
    if (initialized && isAuthenticated && searchParams.get("reset") !== "true") {
      navigate(from, { replace: true });
    }
  }, [initialized, isAuthenticated, from, navigate, searchParams]);

  // Detect ?reset=true in the URL (arrival from the reset-confirm page).
  //
  // The recovery token rides in the query string and is spent by the
  // update-password POST, not by loading this page — so there is no one-time
  // token for an email scanner to burn on prefetch, and nothing to parse out
  // of the URL fragment. The link is unusable only when the token is absent
  // entirely.
  useEffect(() => {
    if (searchParams.get("reset") === "true") {
      setMode("reset");
      if (!searchParams.get("token")) setResetLinkInvalid(true);
    }
  }, [searchParams]);

  // Detect ?mode=signup in URL. Honoured only while registration is open;
  // with signups closed the parameter is ignored so an old link or bookmark
  // still lands somewhere usable rather than on a form that 403s. The flag
  // arrives from the server, so this runs again once /health has answered.
  useEffect(() => {
    if (signupEnabled !== true) return;
    const modeParam = searchParams.get("mode");
    if (modeParam === "signup" && searchParams.get("reset") !== "true") {
      setMode("signup");
    }
  }, [searchParams, signupEnabled]);

  useEffect(() => {
    _authClient.auth.googleEnabled().then(setGoogleEnabled);
  }, []);

  // Surface why a Google round trip failed, instead of bouncing the user back
  // to a blank form with no explanation.
  useEffect(() => {
    const ge = searchParams.get("google_error");
    if (!ge) return;
    const messages: Record<string, string> = {
      not_configured: "Google sign-in isn't configured on this server yet.",
      cancelled: "Google sign-in was cancelled.",
      no_account: "That Google account isn't registered on this instance.",
      email_unverified: "Google hasn't verified that address, so it can't be used to sign in.",
      bad_state: "That sign-in attempt expired. Please try again.",
    };
    setError(messages[ge] ?? "Google sign-in failed. Please try again.");
  }, [searchParams]);

  function validateEmailField(value: string): string | undefined {
    if (!value.trim()) return "Email is required.";
    if (!isValidEmail(value)) return "Please enter a valid email address.";
    return undefined;
  }

  function validatePasswordField(value: string): string | undefined {
    // Sign-in: lenient (existing users may have weaker passwords)
    if (mode === "signin") return validateSignInPassword(value);
    // Signup & reset: enforce full complexity
    return validateNewPassword(value);
  }

  function handleBlur(field: string) {
    setTouched((prev) => ({ ...prev, [field]: true }));
    if (field === "email") {
      const err = validateEmailField(email);
      setFieldErrors((prev) => ({ ...prev, email: err }));
    } else if (field === "password") {
      const err = validatePasswordField(password);
      setFieldErrors((prev) => ({ ...prev, password: err }));
    } else if (field === "newPassword") {
      const err = validatePasswordField(newPassword);
      setFieldErrors((prev) => ({ ...prev, newPassword: err }));
    }
  }

  const handleEmailSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitCooldown > 0) return;
    setError(null);

    const emailErr = validateEmailField(email);
    const passwordErr = validatePasswordField(password);
    setFieldErrors({ email: emailErr, password: passwordErr });
    setTouched({ email: true, password: true });

    if (emailErr || passwordErr) {
      return;
    }

    try {
      if (mode === "signin") {
        await signInWithEmail(email, password);
        // signInWithEmail eagerly sets session/user in the auth store,
        // so isAuthenticated is true immediately. Navigate after yielding
        // one tick to let React process the store update.
        setTimeout(() => navigate(from, { replace: true }), 0);
      } else {
        const result = await signUp(email, password);
        if (result.confirmUrl) {
          setConfirmUrl(result.confirmUrl);
        }
        setSignupSuccess(true);
      }
    } catch (err) {
      // Detect rate limiting (429) and enforce a client-side cooldown
      const status = (err as { status?: number })?.status;
      if (status === 429) {
        setSubmitCooldown(60);
      }
      setError(getFriendlyAuthError(err, mode === "signin" ? "signin" : "signup"));
    }
  };

  const handleForgotPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    const emailErr = validateEmailField(email);
    setFieldErrors({ email: emailErr });
    setTouched({ email: true });

    if (emailErr) {
      return;
    }

    try {
      // The server mints a one-hour recovery token and either emails it (when
      // SMTP is configured) or returns the link in the response for the
      // operator to use — same pattern as /api/auth/signup.
      const resp = await fetch("/api/auth/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const result = await resp.json().catch(() => ({}));

      if (!resp.ok) {
        const err = new Error(result?.error || "Failed to send reset link.");
        (err as { status?: number }).status = resp.status;
        throw err;
      }

      setResetEmailSent(true);
    } catch (err) {
      setError(getFriendlyAuthError(err, "forgot"));
    }
  };

  const handleResetPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    const pwErr = validatePasswordField(newPassword);
    setFieldErrors({ newPassword: pwErr });
    setTouched({ newPassword: true });

    if (pwErr) {
      return;
    }

    try {
      const { error: updateError } = await authClient.auth.updateUser({
        password: newPassword,
        token: searchParams.get("token") ?? undefined,
      });
      if (updateError) throw updateError;
      setPasswordUpdated(true);
      setTimeout(() => {
        navigate("/dashboard", { replace: true });
      }, 2000);
    } catch (err) {
      setError(getFriendlyAuthError(err, "reset"));
    }
  };

  // Countdown timer for resend cooldown
  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = setInterval(() => {
      setResendCooldown((prev) => {
        if (prev <= 1) {
          clearInterval(timer);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [resendCooldown]);

  // Countdown timer for submit cooldown (after 429 rate limit)
  useEffect(() => {
    if (submitCooldown <= 0) return;
    const timer = setInterval(() => {
      setSubmitCooldown((prev) => {
        if (prev <= 1) {
          clearInterval(timer);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [submitCooldown]);

  const handleResend = async () => {
    if (resendCooldown > 0) return;
    setResendStatus("idle");
    try {
      const { error: resendError } = await authClient.auth.resend({
        type: "signup",
        email,
      });
      if (resendError) throw resendError;
      setResendCooldown(60);
      setResendStatus("sent");
      setTimeout(() => setResendStatus("idle"), 5000);
    } catch (err) {
      const msg = err instanceof Error ? err.message.toLowerCase() : "";
      if (msg.includes("rate") || msg.includes("429") || msg.includes("too many")) {
        setResendCooldown(60);
      }
      setResendStatus("error");
      setTimeout(() => setResendStatus("idle"), 5000);
    }
  };

  if (signupSuccess) {
    return (
      <PageWrapper>
        <Card className="border border-primary/10 shadow-warm bg-white">
          <CardHeader className="space-y-1 text-center">
            <div className="mx-auto mb-4">
              <Logo size="lg" className="h-12 w-12" />
            </div>
            <CardTitle className="text-2xl font-bold text-foreground">
              Check your email
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              {confirmUrl
                ? <>Your account has been created. Click the link below to confirm your email and activate your account.</>
                : <>A confirmation link was sent to <strong>{email}</strong>. Click the link to verify your account and sign in.</>
              }
            </CardDescription>
          </CardHeader>
          {import.meta.env.DEV && confirmUrl && (
            <CardContent>
              <a
                href={confirmUrl}
                className="block w-full text-center rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-white hover:bg-primary-hover transition-colors"
              >
                Confirm Email Address (dev)
              </a>
              <p className="mt-2 text-xs text-center text-muted-foreground">
                In production, this link will be sent to your email.
              </p>
            </CardContent>
          )}
          <CardFooter className="flex flex-col gap-2">
            <Button
              variant="ghost"
              className="w-full text-primary hover:text-primary-hover hover:bg-primary/5"
              onClick={() => {
                setSignupSuccess(false);
                setConfirmUrl(null);
                setMode("signin");
              }}
            >
              Back to sign in
            </Button>
            <Button
              variant="ghost"
              className="w-full"
              onClick={handleResend}
              disabled={resendCooldown > 0}
            >
              {resendCooldown > 0
                ? `Resend in ${resendCooldown}s`
                : "Resend verification email"}
            </Button>
            {resendStatus === "sent" && (
              <p className="text-xs text-success">Verification email resent.</p>
            )}
            {resendStatus === "error" && (
              <p className="text-xs text-destructive">Could not resend email. Please try again.</p>
            )}
            <p className="text-xs text-muted-foreground">
              Didn&apos;t receive it? Check your spam folder.
            </p>
          </CardFooter>
        </Card>
      </PageWrapper>
    );
  }

  if (resetEmailSent) {
    return (
      <PageWrapper>
        <Card className="border border-primary/10 shadow-warm bg-white">
          <CardHeader className="space-y-1 text-center">
            <div className="mx-auto mb-4">
              <Logo size="lg" className="h-12 w-12" />
            </div>
            <CardTitle className="text-2xl font-bold text-foreground">
              Check your inbox
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              Password reset link sent to <strong>{email}</strong>. Check your
              inbox.
            </CardDescription>
          </CardHeader>
          <CardFooter>
            <Button
              variant="ghost"
              className="w-full text-primary hover:text-primary-hover hover:bg-primary/5"
              onClick={() => {
                setResetEmailSent(false);
                setMode("signin");
              }}
            >
              Back to sign in
            </Button>
          </CardFooter>
        </Card>
      </PageWrapper>
    );
  }

  if (passwordUpdated) {
    return (
      <PageWrapper>
        <Card className="border border-primary/10 shadow-warm bg-white">
          <CardHeader className="space-y-1 text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-success text-white shadow-warm-sm">
              <CheckCircle className="h-6 w-6" />
            </div>
            <CardTitle className="text-2xl font-bold text-foreground">
              Password updated
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              Password updated successfully. Redirecting to dashboard...
            </CardDescription>
          </CardHeader>
        </Card>
      </PageWrapper>
    );
  }

  if (mode === "reset" && resetLinkInvalid) {
    return (
      <PageWrapper>
        <Card className="border border-primary/10 shadow-warm bg-white">
          <CardHeader className="space-y-1 text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-destructive/10 text-destructive">
              <AlertCircle className="h-6 w-6" />
            </div>
            <CardTitle className="text-2xl font-bold text-foreground">
              Reset link expired
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              This password reset link has expired or was already used. Request
              a new link to continue.
            </CardDescription>
          </CardHeader>
          <CardFooter className="flex flex-col gap-2">
            <Button
              className="w-full"
              onClick={() => {
                setResetLinkInvalid(false);
                setMode("forgot");
                navigate("/login", { replace: true });
              }}
            >
              Request new reset link
            </Button>
            <Button
              variant="ghost"
              className="w-full text-primary hover:text-primary-hover hover:bg-primary/5"
              onClick={() => {
                setResetLinkInvalid(false);
                setMode("signin");
                navigate("/login", { replace: true });
              }}
            >
              Back to sign in
            </Button>
          </CardFooter>
        </Card>
      </PageWrapper>
    );
  }

  if (mode === "reset") {
    return (
      <PageWrapper>
        <Card className="border border-primary/10 shadow-warm bg-white">
          <CardHeader className="space-y-1 text-center">
            <div className="mx-auto mb-4">
              <Logo size="lg" className="h-12 w-12" />
            </div>
            <CardTitle className="text-2xl font-bold text-foreground">
              Set new password
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              Enter your new password below.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleResetPassword} className="space-y-4" noValidate>
              <div className="space-y-2">
                <Label htmlFor="new-password" className="text-foreground">New Password</Label>
                <Input
                  id="new-password"
                  type="password"
                  // The enforced rule, read from the module that enforces it.
                  placeholder={NEW_PASSWORD_HINT}
                  value={newPassword}
                  onChange={(e) => {
                    setNewPassword(e.target.value);
                    if (touched.newPassword) {
                      setFieldErrors((prev) => ({ ...prev, newPassword: validatePasswordField(e.target.value) }));
                    }
                  }}
                  onBlur={() => handleBlur("newPassword")}
                  autoComplete="new-password"
                  aria-invalid={!!fieldErrors.newPassword && touched.newPassword}
                  className={`border-primary/15 focus-visible:ring-primary focus-visible:ring-2 ${
                    fieldErrors.newPassword && touched.newPassword ? "border-destructive focus-visible:ring-destructive" : ""
                  }`}
                />
                {fieldErrors.newPassword && touched.newPassword && (
                  <p className="flex items-center gap-1 text-sm text-destructive">
                    <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                    {fieldErrors.newPassword}
                  </p>
                )}
                {newPassword.length > 0 && (
                  <div className="space-y-1.5">
                    <div className="flex gap-1">
                      {[1, 2, 3, 4, 5].map((i) => (
                        <div
                          key={i}
                          className={`h-1 flex-1 rounded-full ${
                            i <= getPasswordScore(newPassword)
                              ? getPasswordScore(newPassword) <= 2 ? "bg-destructive" : getPasswordScore(newPassword) <= 3 ? "bg-amber-500" : "bg-success"
                              : "bg-gray-300"
                          }`}
                        />
                      ))}
                    </div>
                    <p className={`text-xs ${getPasswordStrengthColor(newPassword)}`}>
                      {getPasswordStrengthLabel(newPassword)}
                    </p>
                    <ul className="space-y-0.5">
                      {getPasswordChecks(newPassword).map((check) => (
                        <li key={check.label} className="flex items-center gap-1.5 text-xs">
                          {check.met ? (
                            <Check className="h-3 w-3 text-success flex-shrink-0" />
                          ) : (
                            <X className="h-3 w-3 text-muted-foreground flex-shrink-0" />
                          )}
                          <span className={check.met ? "text-muted-foreground" : "text-muted-foreground/60"}>
                            {check.label}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>

              {error && (
                <div className="flex items-center gap-2 rounded-lg border border-destructive/20 bg-destructive/5 px-3 py-2">
                  <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0" />
                  <p className="text-sm text-destructive">{error}</p>
                </div>
              )}

              <Button
                type="submit"
                className="w-full bg-primary hover:bg-primary-hover text-white shadow-warm-sm"
              >
                Set New Password
              </Button>
            </form>
          </CardContent>
          <CardFooter>
            <Button
              variant="ghost"
              className="w-full text-primary hover:text-primary-hover hover:bg-primary/5"
              onClick={() => {
                setMode("signin");
                setError(null);
                setFieldErrors({});
                setTouched({});
              }}
            >
              Back to sign in
            </Button>
          </CardFooter>
        </Card>
      </PageWrapper>
    );
  }

  return (
    <PageWrapper>
      <Card className="border border-primary/10 shadow-warm bg-white">
        <CardHeader className="space-y-1 text-center">
          {/* Logo — only show on mobile since desktop has the brand panel */}
          <div className="lg:hidden mx-auto mb-4">
            <Logo size="lg" className="h-12 w-12" />
          </div>
          <CardTitle className="text-2xl font-bold text-foreground">
            {mode === "signin"
              ? "Welcome back"
              : mode === "forgot"
                ? "Reset password"
                : "Create an account"}
          </CardTitle>
          <CardDescription className="text-muted-foreground">
            {mode === "signin"
              ? "Sign in to your Autonomous Accounting account"
              : mode === "forgot"
                ? "Enter your email to start a password reset"
                : "Get started with Autonomous Accounting"}
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-4">
          {mode === "forgot" ? (
            /* Forgot password form -- email only */
            <form onSubmit={handleForgotPassword} className="space-y-4" noValidate>
              <div className="space-y-2">
                <Label htmlFor="email" className="text-foreground">Email</Label>
                <Input
                  id="email"
                  type="email"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    if (touched.email) {
                      setFieldErrors((prev) => ({ ...prev, email: validateEmailField(e.target.value) }));
                    }
                  }}
                  onBlur={() => handleBlur("email")}
                  autoComplete="email"
                  aria-invalid={!!fieldErrors.email && touched.email}
                  className={`border-primary/15 focus-visible:ring-primary focus-visible:ring-2 ${
                    fieldErrors.email && touched.email ? "border-destructive focus-visible:ring-destructive" : ""
                  }`}
                />
                {fieldErrors.email && touched.email && (
                  <p className="flex items-center gap-1 text-sm text-destructive">
                    <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                    {fieldErrors.email}
                  </p>
                )}
              </div>

              {error && (
                <div className="flex items-center gap-2 rounded-lg border border-destructive/20 bg-destructive/5 px-3 py-2">
                  <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0" />
                  <p className="text-sm text-destructive">{error}</p>
                </div>
              )}

              <Button
                type="submit"
                className="w-full bg-primary hover:bg-primary-hover text-white shadow-warm-sm"
              >
                Send Reset Link
              </Button>
            </form>
          ) : (
            <>
              {/* Email / Password form */}
              <form onSubmit={handleEmailSubmit} className="space-y-4" noValidate>
                <div className="space-y-2">
                  <Label htmlFor="email" className="text-foreground">Email</Label>
                  <Input
                    id="email"
                    type="email"
                    placeholder="you@example.com"
                    value={email}
                    onChange={(e) => {
                      setEmail(e.target.value);
                      if (touched.email) {
                        setFieldErrors((prev) => ({ ...prev, email: validateEmailField(e.target.value) }));
                      }
                    }}
                    onBlur={() => handleBlur("email")}
                    autoComplete="email"
                    disabled={loading}
                    aria-invalid={!!fieldErrors.email && touched.email}
                    className={`border-primary/15 focus-visible:ring-primary focus-visible:ring-2 ${
                      fieldErrors.email && touched.email ? "border-destructive focus-visible:ring-destructive" : ""
                    }`}
                  />
                  {fieldErrors.email && touched.email && (
                    <p className="flex items-center gap-1 text-sm text-destructive">
                      <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                      {fieldErrors.email}
                    </p>
                  )}
                </div>
                <div className="space-y-2">
                  <Label htmlFor="password" className="text-foreground">Password</Label>
                  <Input
                    id="password"
                    type="password"
                    // Signing in accepts whatever the account already has, so
                    // only the signup form states the rule — and it states the
                    // one `validateNewPassword` actually enforces.
                    placeholder={mode === "signin" ? "Your password" : NEW_PASSWORD_HINT}
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value);
                      if (touched.password) {
                        setFieldErrors((prev) => ({ ...prev, password: validatePasswordField(e.target.value) }));
                      }
                    }}
                    onBlur={() => handleBlur("password")}
                    autoComplete={
                      mode === "signin" ? "current-password" : "new-password"
                    }
                    disabled={loading}
                    aria-invalid={!!fieldErrors.password && touched.password}
                    className={`border-primary/15 focus-visible:ring-primary focus-visible:ring-2 ${
                      fieldErrors.password && touched.password ? "border-destructive focus-visible:ring-destructive" : ""
                    }`}
                  />
                  {fieldErrors.password && touched.password && (
                    <p className="flex items-center gap-1 text-sm text-destructive">
                      <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                      {fieldErrors.password}
                    </p>
                  )}
                  {mode === "signup" && password.length > 0 && (
                    <div className="space-y-1.5">
                      <div className="flex gap-1">
                        {[1, 2, 3, 4, 5].map((i) => (
                          <div
                            key={i}
                            className={`h-1 flex-1 rounded-full ${
                              i <= getPasswordScore(password)
                                ? getPasswordScore(password) <= 2 ? "bg-destructive" : getPasswordScore(password) <= 3 ? "bg-amber-500" : "bg-success"
                                : "bg-gray-300"
                            }`}
                          />
                        ))}
                      </div>
                      <p className={`text-xs ${getPasswordStrengthColor(password)}`}>
                        {getPasswordStrengthLabel(password)}
                      </p>
                      <ul className="space-y-0.5">
                        {getPasswordChecks(password).map((check) => (
                          <li key={check.label} className="flex items-center gap-1.5 text-xs">
                            {check.met ? (
                              <Check className="h-3 w-3 text-success flex-shrink-0" />
                            ) : (
                              <X className="h-3 w-3 text-muted-foreground flex-shrink-0" />
                            )}
                            <span className={check.met ? "text-muted-foreground" : "text-muted-foreground/60"}>
                              {check.label}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>

                {mode === "signin" && (
                  <div className="flex justify-end">
                    <button
                      type="button"
                      className="text-sm font-medium text-primary underline-offset-4 hover:underline"
                      onClick={() => {
                        setMode("forgot");
                        setError(null);
                        setFieldErrors({});
                        setTouched({});
                      }}
                    >
                      Forgot password?
                    </button>
                  </div>
                )}

                {error && (
                  <div className="flex items-start gap-2 rounded-lg border border-destructive/20 bg-destructive/5 px-3 py-2">
                    <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0 mt-0.5" />
                    <div className="text-sm text-destructive space-y-1">
                      <p>{error}{submitCooldown > 0 && ` (${submitCooldown}s)`}</p>
                      {submitCooldown > 0 && mode === "signup" && (
                        <p className="text-xs text-muted-foreground">
                          Already signed up? Check your email for a confirmation link.
                        </p>
                      )}
                    </div>
                  </div>
                )}

                <Button
                  type="submit"
                  className="w-full bg-primary hover:bg-primary-hover text-white shadow-warm-sm"
                  disabled={loading || submitCooldown > 0}
                >
                  {loading
                    ? "Please wait..."
                    : submitCooldown > 0
                      ? `Wait ${submitCooldown}s`
                      : mode === "signin"
                        ? "Sign in"
                        : "Create account"}
                </Button>
              </form>

              {googleEnabled && mode === "signin" && (
                <>
                  <div className="relative my-5">
                    <div className="absolute inset-0 flex items-center">
                      <Separator className="w-full" />
                    </div>
                    <div className="relative flex justify-center text-xs uppercase">
                      <span className="bg-white px-2 text-muted-foreground">or</span>
                    </div>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    className="w-full border-primary/15 hover:bg-primary/5 text-foreground"
                    onClick={() => _authClient.auth.startGoogleSignIn()}
                  >
                    <svg className="mr-2 h-4 w-4" viewBox="0 0 24 24">
                      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4" />
                      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
                    </svg>
                    Sign in with Google
                  </Button>
                </>
              )}
            </>
          )}
        </CardContent>

        <CardFooter>
          <p className="w-full text-center text-sm text-muted-foreground">
            {mode === "forgot" ? (
              <>
                Remember your password?{" "}
                <button
                  type="button"
                  className="font-medium text-primary underline-offset-4 hover:underline"
                  onClick={() => {
                    setMode("signin");
                    setError(null);
                  }}
                >
                  Sign in
                </button>
              </>
            ) : mode === "signin" ? (
              signupEnabled === true ? (
                <>
                  Don&apos;t have an account?{" "}
                  <button
                    type="button"
                    className="font-medium text-primary underline-offset-4 hover:underline"
                    onClick={() => {
                      setMode("signup");
                      setError(null);
                      setFieldErrors({});
                      setTouched({});
                      setSubmitCooldown(0);
                    }}
                  >
                    Sign up
                  </button>
                </>
              ) : signupEnabled === false ? (
                <span className="text-muted-foreground">
                  This instance is not accepting new accounts.
                </span>
              ) : null
            ) : (
              <>
                Already have an account?{" "}
                <button
                  type="button"
                  className="font-medium text-primary underline-offset-4 hover:underline"
                  onClick={() => {
                    setMode("signin");
                    setError(null);
                    setSubmitCooldown(0);
                  }}
                >
                  Sign in
                </button>
              </>
            )}
          </p>
        </CardFooter>
      </Card>
    </PageWrapper>
  );
}
