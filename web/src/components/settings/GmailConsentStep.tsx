import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Shield, Check, X } from "lucide-react";

interface GmailConsentStepProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onContinue: () => void;
}

export function GmailConsentStep({
  open,
  onOpenChange,
  onContinue,
}: GmailConsentStepProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
              <Shield className="h-5 w-5" />
            </div>
            <DialogTitle className="text-lg">Connect Your Gmail</DialogTitle>
          </div>
          <DialogDescription className="pt-2">
            What this instance reads from your mailbox, and what it keeps.
          </DialogDescription>
        </DialogHeader>

        {/* Consent copy has to match what `core/gather/gmail.py` does, not what
            would be nice to promise: it searches the mailbox, opens the
            messages the search returns, saves their attachments, and renders
            the bodies of matching receipt emails to PDF and keeps those. */}
        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <p className="text-sm font-medium text-foreground">
              What is read
            </p>
            <ul className="space-y-1.5 text-sm text-muted-foreground">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 mt-0.5 text-primary flex-shrink-0" />
                Read-only access to Gmail. Nothing is ever sent, deleted or
                changed in your mailbox.
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 mt-0.5 text-primary flex-shrink-0" />
                A search runs over your mail for the vendors on your watchlist,
                for sent invoices, and for receipt and bill wording. The
                messages that search returns are opened and read in full.
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 mt-0.5 text-primary flex-shrink-0" />
                Mail outside those searches is never fetched — but the search is
                keyword-based, so a personal email that mentions a receipt can
                match.
              </li>
            </ul>
          </div>

          <div className="space-y-2">
            <p className="text-sm font-medium text-foreground">What is stored</p>
            <ul className="space-y-1.5 text-sm text-muted-foreground">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 mt-0.5 text-primary flex-shrink-0" />
                PDF and image attachments that score as financial documents are
                saved to this instance.
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 mt-0.5 text-primary flex-shrink-0" />
                For a receipt email with no attachment, the email body itself is
                rendered to a PDF and saved as a document.
              </li>
              <li className="flex items-start gap-2">
                <X className="h-4 w-4 mt-0.5 text-rose-500 flex-shrink-0" />
                Messages that do not score as financial documents are discarded
                and nothing about them is kept.
              </li>
            </ul>
          </div>

          <p className="rounded-lg bg-muted/50 p-3 text-xs text-muted-foreground">
            Every document saved this way is then read by the extraction
            endpoint this instance is configured with, exactly like a file you
            upload yourself. If that endpoint is a hosted provider, the content
            of those emails and attachments reaches it. Settings &rarr; Security
            names the endpoint this server is actually using. You can disconnect
            at any time and delete the documents that were saved.
          </p>
        </div>

        <DialogFooter className="flex-col gap-2 sm:flex-row">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            className="sm:flex-1"
          >
            Cancel
          </Button>
          <Button onClick={onContinue} className="sm:flex-1 bg-primary hover:bg-primary/90">
            Continue to Google Sign-In
          </Button>
        </DialogFooter>

        <p className="text-center text-xs text-muted-foreground">
          Connecting uses Google&apos;s OAuth flow, so Google sees the sign-in.
          Use of Gmail data here is subject to Google&apos;s Limited Use
          requirements.
        </p>
      </DialogContent>
    </Dialog>
  );
}
