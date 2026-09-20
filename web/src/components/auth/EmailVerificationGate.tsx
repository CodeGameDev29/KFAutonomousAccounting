import { useState, useEffect, useCallback } from "react";
import { Mail } from "lucide-react";
import { Logo } from "@/components/Logo";
import { authClient } from "@/lib/auth-client";
import { useAuthStore } from "@/stores/auth-store";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

const BASE_POLL_MS = 15_000;
const MAX_POLL_MS = 120_000;

/**
 * Full-screen blocking gate shown to email/password users who haven't
 * verified their email address. Polls every 15 seconds, backs off on 429,
 * and pauses while the tab is hidden.
 */
export function EmailVerificationGate() {
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const checkEmailVerification = useAuthStore((s) => s.checkEmailVerification);

  const [resendCooldown, setResendCooldown] = useState(0);
  const [resendStatus, setResendStatus] = useState<"idle" | "sent" | "error">("idle");
  const [pollDelay, setPollDelay] = useState(BASE_POLL_MS);
  const [isHidden, setIsHidden] = useState<boolean>(
    typeof document !== "undefined" ? document.hidden : false,
  );

  const email = user?.email ?? "";

  // Track tab visibility so polling stops while backgrounded
  useEffect(() => {
    const onVisibility = () => setIsHidden(document.hidden);
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  // Auto-poll verification status (pauses while hidden; exponential backoff on 429)
  useEffect(() => {
    if (isHidden) return;
    const interval = setInterval(async () => {
      try {
        const { error } = await authClient.auth.getUser();
        if (error && (error as { status?: number }).status === 429) {
          setPollDelay((prev) => Math.min(prev * 2, MAX_POLL_MS));
          return;
        }
        setPollDelay(BASE_POLL_MS);
        checkEmailVerification();
      } catch {
        // Transient error — keep current cadence
      }
    }, pollDelay);
    return () => clearInterval(interval);
  }, [checkEmailVerification, pollDelay, isHidden]);

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

  const handleResend = useCallback(async () => {
    if (resendCooldown > 0 || !email) return;
    setResendStatus("idle");
    try {
      const { error } = await authClient.auth.resend({
        type: "signup",
        email,
      });
      if (error) throw error;
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
  }, [email, resendCooldown]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 py-12">
      <div className="w-full max-w-sm">
        <Card className="border border-primary/10 shadow-warm bg-white">
          <CardHeader className="space-y-1 text-center">
            <div className="mx-auto mb-4">
              <Logo size="lg" className="h-12 w-12" />
            </div>
            <div className="mx-auto mb-2 flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
              <Mail className="h-6 w-6 text-primary animate-pulse" />
            </div>
            <CardTitle className="text-2xl font-bold text-foreground">
              Verify your email
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              A verification link was sent to{" "}
              <strong className="text-foreground">{email}</strong>. Click the
              link in your inbox to continue.
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-3">
            <Button
              className="w-full bg-primary hover:bg-primary-hover text-white shadow-warm-sm"
              onClick={handleResend}
              disabled={resendCooldown > 0}
            >
              {resendCooldown > 0
                ? `Resend in ${resendCooldown}s`
                : "Resend verification email"}
            </Button>

            {resendStatus === "sent" && (
              <p className="text-xs text-center text-success">
                Verification email resent.
              </p>
            )}
            {resendStatus === "error" && (
              <p className="text-xs text-center text-destructive">
                Could not resend email. Please try again.
              </p>
            )}

            <p className="text-xs text-center text-muted-foreground">
              Check your spam folder if you don't see it.
            </p>
          </CardContent>

          <CardFooter>
            <Button
              variant="ghost"
              className="w-full text-muted-foreground hover:text-foreground"
              onClick={signOut}
            >
              Sign out
            </Button>
          </CardFooter>
        </Card>
      </div>
    </div>
  );
}
