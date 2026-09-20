import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

/**
 * A self-hosted template, not a vendor's policy.
 *
 * Autonomous Accounting has no company behind it and holds nobody's data.
 * "The operator" below means whoever deployed this instance. Every factual
 * claim here is about self-hosted software in general; an operator running an
 * instance for other people should edit this page to describe their own
 * machine, backups and retention before pointing anyone at it.
 */
export function PrivacyPolicy() {
  return (
    <div className="min-h-screen bg-background">
      <div className="container max-w-3xl py-16 px-6">
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-primary transition-colors mb-8"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to home
        </Link>

        <h1 className="text-4xl font-bold tracking-tight text-foreground mb-2">
          Privacy Policy
        </h1>
        <p className="text-sm text-muted-foreground mb-10">
          Template for a self-hosted instance
        </p>

        <div className="prose prose-sm max-w-none text-foreground space-y-6">
          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">1. Who holds your data</h2>
            <p className="leading-relaxed">
              Autonomous Accounting is self-hosted bookkeeping software. This instance is operated by whoever deployed it ("the operator"), and your data lives on whatever machine they run it on. The project's contributors operate nothing, receive nothing and can see nothing.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">2. What is collected</h2>
            <p className="leading-relaxed mb-3">Only what you give it:</p>
            <ul className="list-disc pl-6 space-y-1.5">
              <li>Account information (email address, company name, industry)</li>
              <li>Financial documents you upload (receipts, invoices, bank statements)</li>
              <li>Transaction data extracted from those documents</li>
              <li>Application logs written on the operator&apos;s own machine</li>
            </ul>
            <p className="leading-relaxed mt-3">
              There is no analytics vendor, no error-tracking service and no product telemetry. Nothing is sent anywhere to be counted.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">3. What it is used for</h2>
            <ul className="list-disc pl-6 space-y-1.5">
              <li>Running the bookkeeping pipeline (extraction, reconciliation, reporting)</li>
              <li>Generating CRA-oriented reports and audit binders</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">4. AI processing</h2>
            <p className="leading-relaxed">
              Uploaded documents are read by a language model over an OpenAI-compatible endpoint configured by the operator. Where that endpoint points is the operator&apos;s choice: it may be a model running on their own hardware, in which case nothing leaves their network, or a hosted provider, in which case that provider&apos;s terms apply to what it sees. Settings &rarr; Security reports which provider this instance is actually reaching, read from the server rather than hardcoded. If no provider is reachable, an upload fails rather than being quietly forwarded somewhere else. The application itself has no human review step; whether anyone or anything at the other end reads, retains or trains on a document is governed by that endpoint&apos;s own terms, not by this software.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">5. Storage and security</h2>
            <p className="leading-relaxed">
              Data is stored on the machine the operator self-hosts on, not in managed cloud infrastructure. Row-level security in PostgreSQL keeps each account&apos;s data isolated, and the application drops to an unprivileged database role on every request so those rules are enforced by the database itself. Files are served through short-lived signed URLs. Transport security depends on how the operator exposes the instance.
            </p>
            <p className="leading-relaxed mt-3">
              Being explicit about the limits: data at rest is protected by that machine&apos;s disk encryption and physical security, not by a cloud provider&apos;s managed key infrastructure. The software ships no replication and no point-in-time recovery, so hardware failure or theft is a genuine risk. There is no SOC 2 or equivalent third-party attestation, and there is nobody to issue one. Export everything at any time and keep your own copies.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">6. Retention</h2>
            <p className="leading-relaxed">
              Canadian record-keeping rules (CRA IC05-1R1) expect financial records to be kept for at least six years, and the software is built around that. Actual retention, deletion and backup schedules are set by the operator, since they own the disk.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">7. Your rights under PIPEDA</h2>
            <p className="leading-relaxed mb-3">
              Where the Personal Information Protection and Electronic Documents Act applies, you have the right to:
            </p>
            <ul className="list-disc pl-6 space-y-1.5">
              <li>Access the personal information held about you</li>
              <li>Request correction of inaccurate information</li>
              <li>Withdraw consent for processing</li>
              <li>Request deletion of your data</li>
              <li>Export your data in a portable format</li>
            </ul>
            <p className="leading-relaxed mt-3">
              On a self-hosted instance those requests go to the operator, who has direct access to the database and the export tools.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">8. Third parties</h2>
            <p className="leading-relaxed">
              The software integrates with third parties only where you connect them yourself: an email account for document ingest, a payments or banking source for transaction import, and the AI endpoint described above. Each operates under its own privacy policy. The database and file storage are local to the instance and involve no third party. The software takes no payment, so nothing about you reaches a payment processor.
            </p>
            <p className="leading-relaxed mt-3">
              The pages themselves load nothing from anyone else: no fonts, no scripts, no analytics and no tracking pixels are fetched from an outside domain, so opening this site contacts only the server the operator runs. The one exception is sign-in. If the operator has enabled &ldquo;Sign in with Google&rdquo;, using that button hands your browser to Google to authenticate you, and Google sees that sign-in, and afterwards your Google profile picture is loaded from Google&rsquo;s image servers when the app shows it; the email and password form does not involve anyone but this instance.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">9. Contact</h2>
            <p className="leading-relaxed">
              Privacy questions about this instance go to whoever operates it.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}
