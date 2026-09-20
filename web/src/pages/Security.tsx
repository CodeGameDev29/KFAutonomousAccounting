import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import {
  Lock,
  BrainCircuit,
  EyeOff,
  ShieldCheck,
  FileCheck,
  KeyRound,
} from "lucide-react";
import type { ReactNode } from "react";
import { useSystemInfo, type ExtractionInfo } from "@/hooks/use-system-info";

interface SecurityCardProps {
  icon: ReactNode;
  title: string;
  description: string;
  details: string[];
}

function SecurityCard({ icon, title, description, details }: SecurityCardProps) {
  return (
    <Card className="shadow-warm rounded-xl card-hover">
      <CardHeader className="flex flex-row items-start gap-4 space-y-0">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
          {icon}
        </div>
        <div className="space-y-1">
          <CardTitle className="text-base">{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        <ul className="space-y-1.5 text-sm text-muted-foreground">
          {details.map((detail, i) => (
            <li key={i} className="flex items-start gap-2">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-primary/50" />
              {detail}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

const SECURITY_CARDS: SecurityCardProps[] = [
  {
    icon: <Lock className="h-5 w-5" />,
    title: "Transport and storage",
    description:
      "What protects your data depends on how this instance is hosted.",
    details: [
      "Self-hosted \u2014 your data sits on the machine the operator runs this instance on, not with a cloud provider",
      "TLS in transit, depending on how the operator exposes this instance",
      "Files at rest are protected by that machine's disk encryption and physical security, not by a cloud provider's managed keys",
      "Encrypted credential storage with per-user isolation (per-user derived keys)",
    ],
  },
  {
    icon: <EyeOff className="h-5 w-5" />,
    title: "Unattended Processing",
    description: "This application has no human review step.",
    details: [
      "Fully automated extraction and reconciliation pipeline",
      "No manual data-entry workers and no reviewer queue in this software",
      "What happens at the other end of the extraction endpoint is that endpoint's own policy \u2014 see Document Processing",
    ],
  },
  {
    icon: <ShieldCheck className="h-5 w-5" />,
    title: "Data Privacy",
    description: "Your financial data is private and isolated.",
    details: [
      "Data stored on the operator's own machine, with per-user Row Level Security enforced by the database",
      "Per-user file storage with short-lived signed-URL access control",
      "Your data is isolated from other accounts at the database level",
      "Extraction runs unattended: this software has no reviewer queue",
      "Export and deletion are in the operator's hands, and both tools ship with the software",
      "Built around Canada's PIPEDA (priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/)",
    ],
  },
  {
    icon: <FileCheck className="h-5 w-5" />,
    title: "Record keeping",
    description:
      "Designed around CRA record-keeping guidance — not a certification.",
    details: [
      "Built around the CRA's electronic record-keeping guidance, IC05-1R1 (canada.ca/en/revenue-agency/services/tax/businesses/topics/keeping-records/electronic-records.html). Nobody has certified this software against it — verify with your accountant",
      "Records are kept for as long as the operator keeps them; the CRA expects six years",
      "Exports collect the ledger, receipts and statements for a period into one binder",
      "ZIP exports are unencrypted, so any reviewer can open them",
    ],
  },
  {
    icon: <KeyRound className="h-5 w-5" />,
    title: "Authentication and Access",
    description: "Secure login to protect your account.",
    details: [
      "Email and password sign-in, issued and verified entirely by this instance",
      "Passwords stored as PBKDF2-HMAC-SHA256 hashes with a per-user salt \u2014 never in plain text",
      "Sessions use short-lived tokens with rotating refresh tokens; reusing a rotated token is refused",
      "Your data is isolated from other accounts at the database level via Row Level Security",
    ],
  },
];

/**
 * The document-processing card, built from what the server reports rather than
 * from a string literal.
 *
 * Naming a hosted AI provider unconditionally would be false on any instance
 * whose extraction runs against a self-hosted model, and naming a third party
 * that never sees the data is exactly the class of claim that creates a real
 * liability. So the copy is derived from `/health`'s `extraction` block, and a
 * hosted provider is named only when its key is actually present.
 *
 * `state` separates "not answered yet" from "asked and failed", which a single
 * `extraction === undefined` check cannot. The query is warmed at the app root,
 * and while it is in flight the card says it is still reading rather than
 * claiming nothing is known.
 */
function processingCard(
  extraction: ExtractionInfo | undefined,
  state: "pending" | "error" | "ready",
): SecurityCardProps {
  const icon = <BrainCircuit className="h-5 w-5" />;

  if (!extraction) {
    return {
      icon,
      title: "Document Processing",
      description: "Where your documents are read.",
      details: [
        state === "pending"
          ? "Reading this instance's extraction configuration from the server…"
          : state === "error"
            ? "The server could not be reached, so nothing is claimed here. It answers on /health once it is up."
            : "This server reported no extraction configuration, so nothing is claimed here.",
      ],
    };
  }

  const local = extraction.local;
  const localIsPrimary = extraction.primary === "local" && !!local?.enabled;
  const gemini = extraction.gemini_key_present === true;
  const openai = extraction.openai_key_present === true;

  const details: string[] = [];

  if (localIsPrimary && local) {
    // Report the endpoint the server actually returned and nothing more: where
    // it points is the operator's choice, and this page must not guess.
    details.push(
      `Documents are read by a model on an OpenAI-compatible endpoint configured by the operator (${local.base_url}, model "${local.model}").`,
    );
    if (!gemini && !openai) {
      details.push(
        "No hosted fallback is configured. If that endpoint is unreachable an upload fails outright — it is never sent to a third party instead.",
      );
    }
    if (local.reachable === false) {
      details.push(
        "That endpoint is not answering right now, so uploads will fail until it is back.",
      );
    }
  } else if (extraction.primary === null) {
    details.push(
      "No extraction provider is configured on this instance, so uploads cannot be read at all. Nothing is sent anywhere.",
    );
  }

  // A hosted fallback is named only when its key is actually present, and
  // nothing is said about what that provider does with a document: retention
  // and training are its policy, under whatever terms the operator agreed to,
  // and this software cannot know or promise them.
  if (gemini) {
    details.push(
      localIsPrimary
        ? "If the configured endpoint is unavailable, a document is sent to Google Gemini instead"
        : "Documents are sent to Google Gemini to be read",
    );
  }
  if (openai) {
    details.push("As a last resort, a document is sent to OpenAI to be read");
  }

  details.push("Extracted data stays in your account, on the machine this instance runs on");
  details.push(
    "Whether a document is retained or trained on is governed by whichever endpoint the operator configured, not by this software — read that endpoint's own terms",
  );

  return {
    icon,
    title: localIsPrimary ? "Self-Hosted Document Processing" : "Document Processing",
    description: localIsPrimary
      ? "Your documents are read by a model on an endpoint the operator configured, not by a third-party AI provider."
      : "Where your documents are sent to be read, as this server reports it.",
    details,
  };
}

/** Embeddable security content used as a tab within Settings. */
export function SecurityTab() {
  const { data: system, isPending, isError } = useSystemInfo();
  const cards = [
    SECURITY_CARDS[0],
    processingCard(
      system?.extraction,
      isPending ? "pending" : isError ? "error" : "ready",
    ),
    ...SECURITY_CARDS.slice(1),
  ];

  return (
    <div className="space-y-6">
      {/* What the software does, and what is the operator's to decide. No
          heading here speaks for a company: there is none. */}
      <div>
        <h3 className="text-lg font-semibold tracking-tight mb-4">
          What protects your data
        </h3>
        <p className="text-muted-foreground mb-4">
          Some of this is what the software does; the rest depends on the
          machine the operator runs it on.
        </p>
        <div className="grid gap-6 md:grid-cols-2">
          {cards.map((card) => (
            <SecurityCard key={card.title} {...card} />
          ))}
        </div>
      </div>

      <Separator />

      {/* Contact footer */}
      <div className="rounded-xl border border-primary/10 bg-primary/[0.02] px-6 py-4 text-center text-sm text-muted-foreground shadow-warm-sm">
        <p>
          A security concern about this instance goes to whoever operates it. A
          vulnerability in the software itself belongs in the project&apos;s
          issue tracker.
        </p>
      </div>
    </div>
  );
}

/** Redirect /security to /settings?tab=security for backwards compatibility. */
export function Security() {
  const navigate = useNavigate();

  useEffect(() => {
    navigate("/settings?tab=security", { replace: true });
  }, [navigate]);

  return null;
}
