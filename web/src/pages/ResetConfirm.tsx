import { useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { ShieldCheck, AlertCircle } from "lucide-react";
import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/button";
/**
 * Intermediate landing page that confirms a password-reset click.
 *
 * The reset token is spent by the update-password POST rather than by loading
 * a page, so an email-safety scanner that prefetches the link cannot consume
 * it. This page is the confirmation step in front of that POST: it asks for an
 * unmistakable "yes, I asked for this" click before a password change, and it
 * keeps the token out of a referrer header on the way to the form.
 */
export function ResetConfirm() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [redirecting, setRedirecting] = useState(false);

  const token = searchParams.get("token") || "";

  const handleContinue = () => {
    if (!token) return;
    setRedirecting(true);
    navigate(`/login?reset=true&token=${encodeURIComponent(token)}`, {
      replace: true,
    });
  };

  const invalid = !token;

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <div className="w-full max-w-md">
        <div className="bg-card border border-border rounded-2xl shadow-lg p-8">
          <div className="flex flex-col items-center text-center mb-6">
            <Logo className="h-10 w-10 mb-4" />
            {invalid ? (
              <>
                <div className="h-12 w-12 rounded-full bg-destructive/10 text-destructive flex items-center justify-center mb-3">
                  <AlertCircle className="h-6 w-6" />
                </div>
                <h1 className="text-xl font-semibold text-foreground mb-2">
                  Invalid reset link
                </h1>
                <p className="text-sm text-muted-foreground">
                  This link is missing or malformed. Please request a new
                  password reset email.
                </p>
              </>
            ) : (
              <>
                <div className="h-12 w-12 rounded-full bg-primary/10 text-primary flex items-center justify-center mb-3">
                  <ShieldCheck className="h-6 w-6" />
                </div>
                <h1 className="text-xl font-semibold text-foreground mb-2">
                  Confirm password reset
                </h1>
                <p className="text-sm text-muted-foreground">
                  Click below to continue. This extra step protects your
                  link from being consumed by email-safety scanners.
                </p>
              </>
            )}
          </div>

          {invalid ? (
            <Button
              className="w-full"
              onClick={() => navigate("/login", { replace: true })}
            >
              Back to sign in
            </Button>
          ) : (
            <Button
              className="w-full"
              onClick={handleContinue}
              disabled={redirecting}
            >
              {redirecting ? "Redirecting…" : "Continue to reset password"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
