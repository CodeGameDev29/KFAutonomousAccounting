import { useNavigate, Link } from "react-router-dom";
import { ArrowRight, HardDrive, LogOut } from "lucide-react";
import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/use-auth";
import { useSignupEnabled } from "@/hooks/use-system-info";

/**
 * The public front door of an Autonomous Accounting instance.
 *
 * This page sells nothing and speaks for nobody: the software is self-hosted,
 * and whoever deployed this copy is the only party behind it. So a stranger
 * who lands here gets the name, one sentence of what the software does, a
 * sign-in door, whether this instance takes new accounts (read from the
 * server, not baked into the bundle), and a plain statement of where the data
 * sits — including the parts the software does not do for you.
 *
 * `/sample-report` renders a report built from invented figures; it is
 * deliberately not advertised here.
 */

/**
 * Custody facts, stated plainly.
 *
 * Every line here describes what the *software* does on the machine it runs
 * on. Nothing claims a practice that depends on the operator: the software
 * cannot know whether the disk under it is encrypted or whether the backup is
 * copied anywhere, so this page does not say that either is true.
 */
const CUSTODY_FACTS: { label: string; value: string }[] = [
  {
    label: "Hardware",
    value:
      "Whatever machine the operator runs it on. There is no vendor cloud in the path and no account with anyone.",
  },
  {
    label: "Certification",
    value: "None. No SOC 2, no third-party security audit, no compliance programme.",
  },
  {
    label: "Encryption at rest",
    value:
      "The application does not encrypt the database or the stored documents. They sit on the host's own disk, protected by whatever the operator's disk and OS provide.",
  },
  {
    label: "Backups",
    value:
      "The operator's responsibility. A helper script ships (scripts/local/backup-aa.ps1) and can be registered as a daily task, but nothing runs unless the operator sets it up, and a copy on the same disk is not off-site recovery.",
  },
];

export function Landing() {
  const navigate = useNavigate();
  const { isAuthenticated, signOut } = useAuth();
  const signupEnabled = useSignupEnabled();

  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-border/50 bg-background/90 backdrop-blur-md supports-[backdrop-filter]:bg-background/80">
        <div className="container flex h-16 items-center justify-between">
          <div className="flex items-center gap-2.5">
            <Logo size="md" />
            <span className="text-base font-semibold tracking-tight text-foreground sm:text-lg">
              Autonomous Accounting
            </span>
          </div>
          {isAuthenticated ? (
            <div className="flex items-center gap-2 sm:gap-3">
              <Button
                variant="ghost"
                onClick={() => signOut()}
                className="text-muted-foreground hover:bg-destructive/5 hover:text-destructive"
              >
                <LogOut className="mr-2 h-4 w-4" />
                Sign out
              </Button>
              <Button
                onClick={() => navigate("/dashboard")}
                className="bg-primary text-white shadow-warm-sm hover:bg-primary/90"
              >
                Go to Dashboard
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            </div>
          ) : (
            <Button
              variant="ghost"
              onClick={() => navigate("/login")}
              className="text-muted-foreground hover:bg-primary/5 hover:text-primary"
            >
              Sign in
            </Button>
          )}
        </div>
      </header>

      <main className="flex flex-1 items-start justify-center px-6 py-16 sm:items-center sm:py-20">
        <div className="w-full max-w-xl">
          {/* Eyebrow — the one mono detail, so the page reads like a record
              rather than an ad. */}
          <div className="flex items-center gap-3">
            <span aria-hidden="true" className="h-px w-8 bg-primary/60" />
            <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
              Self-hosted bookkeeping
            </p>
          </div>

          <h1 className="mt-5 text-4xl font-bold leading-[1.1] tracking-tight text-foreground sm:text-5xl">
            Autonomous Accounting
          </h1>

          <p className="mt-5 text-lg leading-relaxed text-muted-foreground">
            Receipts and bank statements in, a reconciled double-entry ledger out —
            running entirely on hardware you control.
          </p>

          <div className="mt-9">
            <Button
              size="lg"
              onClick={() => navigate(isAuthenticated ? "/dashboard" : "/login")}
              className="w-full bg-primary px-8 py-6 text-base text-white shadow-warm-md transition-all duration-200 hover:bg-primary/90 sm:w-auto"
            >
              {isAuthenticated ? "Go to dashboard" : "Sign in"}
              <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </div>

          <p className="mt-4 text-sm text-muted-foreground">
            This instance is operated by whoever deployed it — not by the authors of
            the software.
            {signupEnabled === false && " It is not accepting new accounts."}
            {signupEnabled === true && " Registration is open."}
          </p>

          <section
            aria-labelledby="data-custody"
            className="mt-12 rounded-xl border border-border/60 bg-card p-6 shadow-warm-sm sm:mt-14 sm:p-7"
          >
            <div className="flex items-center gap-3">
              <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-success/10">
                <HardDrive className="h-4 w-4 text-success" />
              </span>
              <h2 id="data-custody" className="text-sm font-semibold text-foreground">
                Where the data lives
              </h2>
            </div>

            <dl className="mt-6 divide-y divide-border/50">
              {CUSTODY_FACTS.map((fact) => (
                <div
                  key={fact.label}
                  className="grid gap-1.5 py-4 first:pt-0 last:pb-0 sm:grid-cols-[10rem_1fr] sm:gap-5"
                >
                  <dt className="font-mono text-[11px] uppercase leading-5 tracking-[0.14em] text-muted-foreground">
                    {fact.label}
                  </dt>
                  <dd className="text-sm leading-relaxed text-foreground">{fact.value}</dd>
                </div>
              ))}
            </dl>
          </section>
        </div>
      </main>

      <footer className="border-t border-border/60 bg-background">
        <div className="container flex flex-col items-center justify-between gap-3 py-8 text-xs text-muted-foreground/80 sm:flex-row">
          <p className="font-mono">Autonomous Accounting</p>
          <nav className="flex items-center gap-5">
            <Link to="/privacy" className="transition-colors hover:text-primary">
              Privacy
            </Link>
            <Link to="/terms" className="transition-colors hover:text-primary">
              Terms
            </Link>
          </nav>
        </div>
      </footer>
    </div>
  );
}
