import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

/**
 * A self-hosted template, not a contract.
 *
 * Autonomous Accounting is software you run yourself. There is no vendor, no
 * company behind this page and nobody to sue — "the operator" below means
 * whoever deployed this instance, which in most cases is the reader. If you
 * run an instance for other people, replace this page with terms that describe
 * your actual arrangement; nothing here has been reviewed by a lawyer.
 */
export function TermsOfService() {
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
          Terms of Service
        </h1>
        <p className="text-sm text-muted-foreground mb-10">
          Template for a self-hosted instance
        </p>

        <div className="prose prose-sm max-w-none text-foreground space-y-6">
          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">1. Who operates this instance</h2>
            <p className="leading-relaxed">
              This instance of Autonomous Accounting is operated by whoever deployed it ("the operator"). Autonomous Accounting is open-source software: the people who wrote it do not run this instance, do not hold your data and are not a party to these terms. If you are reading this on a machine you administer, you are the operator.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">2. Description of the software</h2>
            <p className="leading-relaxed">
              Autonomous Accounting is self-hosted double-entry bookkeeping software aimed at Canadian small businesses. It extracts receipts and invoices, reconciles documents against bank transactions, and generates CRA-oriented reports and audit binders. It is not a substitute for professional accounting advice.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">3. Accounts</h2>
            <p className="leading-relaxed">
              Accounts exist only on this instance and are created by the operator. You are responsible for keeping your credentials secure and for activity under your account.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">4. No payment</h2>
            <p className="leading-relaxed">
              The software takes no payment of any kind, and every signed-in account has full access to every feature. If an operator charges for access to their own instance, that arrangement is between them and their users and is described nowhere in this software.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">5. Your data</h2>
            <p className="leading-relaxed">
              You retain ownership of everything you upload. It is processed only to run the software for you. You can export it at any time (PDF, XLSX, ZIP audit binder). Deletion, retention and backups are entirely in the operator's hands — see the Privacy Policy for what that means in practice.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">6. Accuracy disclaimer</h2>
            <p className="leading-relaxed">
              Extraction and reconciliation use AI-based processing and can be wrong. You are responsible for reviewing and verifying every extracted figure, every match and every generated report before filing with a tax authority or making a financial decision.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">7. Acceptable use</h2>
            <p className="leading-relaxed">
              Use the software for lawful bookkeeping. Do not upload content that breaks applicable law, and do not attempt to reach data belonging to another account on this instance.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">8. No warranty, no liability</h2>
            <p className="leading-relaxed">
              The software is provided "as is", without warranty of any kind, as stated in the licence that accompanies it. To the maximum extent permitted by law, neither the operator nor the project's contributors are liable for any indirect, incidental or consequential damages arising from its use — including assessments, penalties or interest resulting from reliance on generated reports.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">9. Changes</h2>
            <p className="leading-relaxed">
              The operator may edit this page at any time, because on a self-hosted instance it is simply a file in the repository.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">10. Governing law</h2>
            <p className="leading-relaxed">
              Any dispute is governed by the law of the jurisdiction in which the operator of this instance is established. The software itself is distributed under its open-source licence, which governs the code rather than this deployment.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-foreground mb-3">11. Contact</h2>
            <p className="leading-relaxed">
              Questions about this instance go to whoever operates it. Questions about the software itself belong in the project's issue tracker.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}
