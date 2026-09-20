"""Gmail API document gathering — multi-pass search for maximum coverage.

Pass 1: Vendor emails with PDF/image attachments (telecom, utilities, cloud providers, etc.)
Pass 2: Sent folder — invoices the account owner emailed to clients
Pass 3: Receipt/bill emails without attachments — save email body as PDF (an app
        store, a telecom, etc.)
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from core.gather.doc_scorer import score_document

logger = logging.getLogger(__name__)

DEFAULT_VENDOR_WATCHLIST = [
    # Example vendor entries — replace with your own suppliers.
    {"name": "Payment processor", "domains": ["payments.example.com"]},
    {"name": "Telecom", "domains": ["telecom.example.com"]},
    {"name": "Electricity utility", "domains": ["utility.example.com"]},
    {"name": "Gas utility", "domains": ["gas.example.com"]},
    {"name": "Online retailer", "domains": ["retailer.example.com"]},
]

ATTACHMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}


def _gather_mirror_dir() -> Path:
    """Second, human-browsable copy of everything gathered.

    Same setting the orchestrator uses: ``GATHER_MIRROR_DIR``, defaulting to
    ``<repo>/data/gather-mirror`` so the path resolves with no configuration on
    any OS.
    """
    mirror = os.environ.get("GATHER_MIRROR_DIR")
    if not mirror:
        mirror = str(Path(__file__).resolve().parents[2] / "data" / "gather-mirror")
    return Path(mirror)


def _date_to_epoch(dt: datetime) -> int:
    """Convert datetime to epoch seconds for Gmail API query accuracy.

    Using epoch seconds avoids timezone offset issues with the YYYY/MM/DD
    format which Gmail interprets as PST regardless of user timezone.
    """
    return int(calendar.timegm(dt.utctimetuple()))


def _is_retryable_error(exc: BaseException) -> bool:
    """Don't retry on invalid_grant (token revoked) — it will never succeed."""
    error_str = str(exc).lower()
    if "invalid_grant" in error_str:
        return False
    return True


@dataclass
class GatherResult:
    source: str
    documents_new: int = 0
    documents_duplicate: int = 0
    documents_filtered: int = 0
    transactions_new: int = 0
    transactions_duplicate: int = 0
    errors: list[str] = field(default_factory=list)
    scores: list[dict] = field(default_factory=list)  # DocumentScore.to_dict() for each file


class GmailGatherer:
    """Gathers documents from Gmail using multi-pass search."""

    def __init__(self, credentials_store, db, output_dir: str | Path) -> None:
        self.credentials_store = credentials_store
        self.db = db
        self.output_dir = Path(output_dir) / "gmail"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

    # ── OAuth ─────────────────────────────────────────────────────

    def _get_oauth_config(self) -> tuple[str, str]:
        client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        return client_id, client_secret

    def _get_redirect_uri(self) -> str:
        explicit = os.getenv("GMAIL_REDIRECT_URI", "")
        if explicit:
            return explicit
        base = os.getenv("BASE_URL", "http://localhost:8080")
        return f"{base.rstrip('/')}/api/onboarding/gmail/callback"

    def _build_client_config(self, redirect_uri: str) -> dict:
        client_id, client_secret = self._get_oauth_config()
        return {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }

    def start_oauth_flow(self, state: str) -> str | None:
        """Generate Google OAuth URL. Stateless -- no self._pending_flow."""
        client_id, client_secret = self._get_oauth_config()
        if not client_id or not client_secret:
            return None
        try:
            from google_auth_oauthlib.flow import Flow
            redirect_uri = self._get_redirect_uri()
            flow = Flow.from_client_config(
                self._build_client_config(redirect_uri),
                scopes=self.GMAIL_SCOPES,
            )
            flow.redirect_uri = redirect_uri
            auth_url, _ = flow.authorization_url(
                access_type="offline", prompt="consent", state=state,
            )
            return auth_url
        except Exception as e:
            logger.error("Failed to start OAuth flow: %s", e)
            return None

    @classmethod
    def exchange_code_for_tokens(cls, code: str, redirect_uri: str | None = None):
        """Exchange auth code for OAuth credentials. Classmethod -- no instance needed."""
        client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            return None
        if redirect_uri is None:
            explicit = os.getenv("GMAIL_REDIRECT_URI", "")
            if explicit:
                redirect_uri = explicit
            else:
                base = os.getenv("BASE_URL", "http://localhost:8080")
                redirect_uri = f"{base.rstrip('/')}/api/onboarding/gmail/callback"
        try:
            from google_auth_oauthlib.flow import Flow
            flow = Flow.from_client_config(
                {"web": {
                    "client_id": client_id, "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }},
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            )
            flow.redirect_uri = redirect_uri
            flow.fetch_token(code=code)
            return flow.credentials
        except Exception as e:
            logger.error("Failed to exchange OAuth code: %s", e)
            return None

    def is_connected(self) -> bool:
        return self.credentials_store.retrieve("gmail_refresh_token") is not None

    def get_connected_email(self) -> str | None:
        """Get the email address of the connected Gmail account."""
        if not self.is_connected():
            return None
        try:
            service = self._get_service()
            profile = service.users().getProfile(userId="me").execute()
            return profile.get("emailAddress")
        except Exception as e:
            logger.warning("Could not get Gmail profile email: %s", e)
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    def _get_service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client_id, client_secret = self._get_oauth_config()
        refresh_token = self.credentials_store.retrieve("gmail_refresh_token")
        if not refresh_token:
            raise RuntimeError("Gmail not connected — no refresh token")

        creds = Credentials(
            token=self.credentials_store.retrieve("gmail_access_token"),
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        if creds.expired or not creds.valid:
            creds.refresh(Request())
            self.credentials_store.store("gmail_access_token", creds.token)
            if creds.expiry:
                self.credentials_store.store("gmail_token_expiry", creds.expiry.isoformat())

        return build("gmail", "v1", credentials=creds)

    # ── Search helpers ────────────────────────────────────────────

    def _get_watchlist(self) -> list[dict]:
        sources = self.db.get_gather_sources()
        for src in sources:
            if src["source_type"] == "gmail" and src.get("config_json"):
                wl = src["config_json"].get("vendor_watchlist")
                if wl:
                    return wl
        return DEFAULT_VENDOR_WATCHLIST

    def _date_range_query(self, since: datetime, until: datetime | None = None) -> str:
        """Build the after:/before: date range portion of a Gmail query."""
        parts = [f"after:{_date_to_epoch(since)}"]
        if until is not None:
            parts.append(f"before:{_date_to_epoch(until)}")
        return " ".join(parts)

    def _build_vendor_query(self, watchlist: list[dict], since: datetime, until: datetime | None = None) -> str:
        """Pass 1: vendor emails with attachments."""
        from_clauses = []
        for vendor in watchlist:
            for domain in vendor.get("domains", []):
                from_clauses.append(f"from:@{domain}")
        parts = []
        if from_clauses:
            parts.append(f"({' OR '.join(from_clauses)})")
        parts.append("has:attachment")
        parts.append(self._date_range_query(since, until))
        return " ".join(parts)

    def _build_sent_invoice_query(self, since: datetime, until: datetime | None = None) -> str:
        """Pass 2: sent emails with invoice/receipt attachments."""
        return (
            f"in:sent (subject:invoice OR subject:receipt OR subject:statement) "
            f"has:attachment {self._date_range_query(since, until)}"
        )

    def _build_body_receipt_query(self, watchlist: list[dict], since: datetime, until: datetime | None = None) -> str:
        """Pass 3: receipt/bill emails without attachments (save body as PDF)."""
        from_clauses = []
        for vendor in watchlist:
            for domain in vendor.get("domains", []):
                from_clauses.append(f"from:@{domain}")
        parts = []
        if from_clauses:
            parts.append(f"({' OR '.join(from_clauses)})")
        parts.append("(subject:receipt OR subject:bill OR subject:invoice OR subject:order)")
        parts.append("-has:attachment")
        parts.append(self._date_range_query(since, until))
        return " ".join(parts)

    # ── Message listing ───────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    def _list_messages(self, service, query: str) -> list[dict]:
        """List all messages matching query. Uses maxResults=500 to reduce API calls."""
        messages = []
        for msg in self._iter_messages(service, query):
            messages.append(msg)
        return messages

    def _iter_messages(self, service, query: str) -> Iterator[dict]:
        """Generator-based message listing for memory efficiency on large inboxes."""
        page_token = None
        while True:
            resp = (
                service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token, maxResults=500)
                .execute()
            )
            for msg in resp.get("messages", []):
                yield msg
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ── Document scoring & validation ────────────────────────────

    _JUNK_SUBJECT_PATTERN = re.compile(
        r'add checkout|payment buttons|payment links|get paid fast'
        r'|how you invoice|unsubscribe.*offer|marketing'
        r'|verify your email|confirm your email|reset your password'
        r'|job application|application received'
        r'|shipment details|tracking number'
        r'|account activity|new payment method'
        r'|automatic reply'
        r'|get up to.*when you transfer|order confirmation'
        r'|identity confirmation request'
        r'|order was collected|order was delivered',
        re.IGNORECASE,
    )

    def _score_and_validate(self, data: bytes, filename: str, result: GatherResult) -> bool:
        """Score a document and decide whether to include it.

        Returns True if the document should be saved, False to skip.
        Records the score in result.scores either way.
        """
        doc_score = score_document(data, filename)
        result.scores.append(doc_score.to_dict())

        if not doc_score.included:
            logger.info("Filtered %s — %s (score %.2f)", filename, doc_score.rejection_reason, doc_score.final_score)
            result.documents_filtered += 1
            return False
        return True

    def _score_and_validate_text(self, text: str, subject: str, result: GatherResult) -> bool:
        """Score email body text before converting to PDF.

        Returns True if the email should be saved, False to skip.
        """
        # Pre-filter: junk subjects never even get scored
        if subject and self._JUNK_SUBJECT_PATTERN.search(subject):
            result.documents_filtered += 1
            return False

        # Score using the text directly
        dummy_filename = f"Gmail - {subject[:40]}.pdf"
        doc_score = score_document(b"", dummy_filename, text=text)
        result.scores.append(doc_score.to_dict())

        if not doc_score.included:
            logger.info("Filtered email '%s' — %s (score %.2f)", subject[:50], doc_score.rejection_reason, doc_score.final_score)
            result.documents_filtered += 1
            return False
        return True

    # ── Attachment download ───────────────────────────────────────

    def _process_attachments(self, service, messages: list[dict], result: GatherResult) -> None:
        """Download PDF/image attachments from a list of messages."""
        for msg_meta in messages:
            msg_id = msg_meta["id"]
            try:
                msg = service.users().messages().get(userId="me", id=msg_id).execute()
            except Exception as e:
                result.errors.append(f"Failed to get message {msg_id}: {e}")
                continue

            parts = self._get_all_parts(msg.get("payload", {}))
            for part in parts:
                filename = part.get("filename", "")
                if not filename:
                    continue
                ext = Path(filename).suffix.lower()
                if ext not in ATTACHMENT_EXTENSIONS:
                    continue
                attachment_id = part.get("body", {}).get("attachmentId")
                if not attachment_id:
                    continue

                source_id = f"{msg_id}:{attachment_id}"
                if self.db.get_gather_log_by_source_id("gmail", source_id):
                    result.documents_duplicate += 1
                    continue

                try:
                    att = (
                        service.users()
                        .messages()
                        .attachments()
                        .get(userId="me", messageId=msg_id, id=attachment_id)
                        .execute()
                    )
                    data = base64.urlsafe_b64decode(att["data"])
                except Exception as e:
                    result.errors.append(f"Failed to download {filename}: {e}")
                    continue

                # Score and validate document
                if not self._score_and_validate(data, filename, result):
                    continue

                file_hash = hashlib.sha256(data).hexdigest()
                if self.db.get_gather_log_by_hash(file_hash) or self.db.is_file_processed(file_hash):
                    result.documents_duplicate += 1
                    continue

                dest = self._save_file(filename, data)
                self.db.insert_gather_log(
                    source="gmail", source_id=source_id,
                    filename=dest.name, file_hash=file_hash, file_size=len(data),
                    stored_path=str(dest),
                    metadata_json={"message_id": msg_id, "original_filename": filename},
                )
                result.documents_new += 1

    def _get_all_parts(self, payload: dict) -> list[dict]:
        """Recursively extract all MIME parts (handles nested multipart)."""
        parts = []
        if payload.get("filename"):
            parts.append(payload)
        for sub in payload.get("parts", []):
            parts.extend(self._get_all_parts(sub))
        return parts

    # ── Email body to PDF ─────────────────────────────────────────

    def _process_body_emails(self, service, messages: list[dict], result: GatherResult) -> None:
        """Save email body as PDF for emails without attachments."""
        for msg_meta in messages:
            msg_id = msg_meta["id"]
            source_id = f"{msg_id}:body"

            if self.db.get_gather_log_by_source_id("gmail", source_id):
                result.documents_duplicate += 1
                continue

            try:
                msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            except Exception as e:
                result.errors.append(f"Failed to get message {msg_id}: {e}")
                continue

            # Extract subject for filename
            headers = msg.get("payload", {}).get("headers", [])
            subject = ""
            for h in headers:
                if h["name"].lower() == "subject":
                    subject = h["value"]
                    break

            # Extract body text (plain) for scoring + raw HTML for rendering
            body_text = self._extract_body_text(msg.get("payload", {}))
            raw_html = self._extract_raw_html(msg.get("payload", {}))
            if not body_text or len(body_text.strip()) < 20:
                continue

            # Score and validate email body
            if not self._score_and_validate_text(body_text, subject, result):
                continue

            # Also extract sender/date for proper email formatting
            sender = ""
            email_date = ""
            for h in headers:
                if h["name"].lower() == "from":
                    sender = h["value"]
                elif h["name"].lower() == "date":
                    email_date = h["value"]

            # Generate properly rendered PDF from HTML
            safe_subject = re.sub(r'[^\w\s\-]', '', subject)[:80].strip() or f"email_{msg_id[:8]}"
            filename = f"Gmail - {safe_subject}.pdf"
            file_data = self._email_to_pdf(subject, sender, email_date, body_text, raw_html)
            if not file_data:
                continue

            file_hash = hashlib.sha256(file_data).hexdigest()
            if self.db.get_gather_log_by_hash(file_hash) or self.db.is_file_processed(file_hash):
                result.documents_duplicate += 1
                continue

            dest = self._save_file(filename, file_data)
            self.db.insert_gather_log(
                source="gmail", source_id=source_id,
                filename=dest.name, file_hash=file_hash, file_size=len(file_data),
                stored_path=str(dest),
                metadata_json={"message_id": msg_id, "subject": subject, "type": "body_pdf"},
            )
            result.documents_new += 1

    def _extract_body_text(self, payload: dict) -> str:
        """Extract plain text from email for scoring (strips HTML)."""
        import html as html_mod
        raw = self._extract_raw_html(payload)
        if not raw:
            return ""
        # Strip HTML tags, decode entities
        text = re.sub(r'<[^>]+>', ' ', raw)
        text = html_mod.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _extract_raw_html(self, payload: dict) -> str:
        """Extract raw HTML body from email (for rendering)."""
        # Prefer HTML version
        if payload.get("mimeType") == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Fall back to plain text wrapped in basic HTML
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                return f"<pre style='font-family:sans-serif;font-size:11px;white-space:pre-wrap'>{escaped}</pre>"

        # Recurse into multipart
        for part in payload.get("parts", []):
            html = self._extract_raw_html(part)
            if html:
                return html
        return ""

    def _email_to_pdf(
        self, subject: str, sender: str, date_str: str, body_text: str,
        raw_html: str = "",
    ) -> bytes | None:
        """Render email to PDF using PyMuPDF Story (HTML renderer).

        If raw_html is provided, renders the styled HTML.
        Otherwise falls back to plain text rendering.
        """
        import html as html_mod
        try:
            import os
            import tempfile

            import fitz

            # Build full HTML document with email header
            esc_from = html_mod.escape(sender)
            esc_date = html_mod.escape(date_str)
            esc_subj = html_mod.escape(subject)

            email_header = f"""
            <div style="font-family:Arial,sans-serif;font-size:10px;color:#555;
                        border-bottom:1px solid #ccc;padding-bottom:8px;margin-bottom:12px">
                <div><b>From:</b> {esc_from}</div>
                <div><b>Date:</b> {esc_date}</div>
                <div><b>Subject:</b> {esc_subj}</div>
            </div>
            """

            if raw_html:
                # Clean up the raw HTML for rendering
                body_html = raw_html
                # Remove <html>, <head>, <body> wrappers — Story handles the document
                body_html = re.sub(r'<html[^>]*>|</html>', '', body_html, flags=re.IGNORECASE)
                body_html = re.sub(r'<head>.*?</head>', '', body_html, flags=re.IGNORECASE | re.DOTALL)
                body_html = re.sub(r'<body[^>]*>|</body>', '', body_html, flags=re.IGNORECASE)
                # Remove scripts
                body_html = re.sub(r'<script[^>]*>.*?</script>', '', body_html, flags=re.IGNORECASE | re.DOTALL)
                # Fix image references (remove external images to avoid errors)
                body_html = re.sub(r'<img[^>]*>', '', body_html, flags=re.IGNORECASE)
            else:
                # Plain text fallback
                escaped = html_mod.escape(body_text)
                body_html = f"<pre style='font-family:sans-serif;font-size:10px;white-space:pre-wrap'>{escaped}</pre>"

            full_html = f"""
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;
                        color:#333;max-width:540px;margin:0 auto">
                {email_header}
                {body_html}
            </div>
            """

            # Simplify HTML for Story renderer
            simple_html = full_html
            # Strip <style> blocks and inline styles
            simple_html = re.sub(r'<style[^>]*>.*?</style>', '', simple_html, flags=re.IGNORECASE | re.DOTALL)
            simple_html = re.sub(r'\sstyle="[^"]*"', '', simple_html)
            # Remove empty tables/divs that cause blank pages
            simple_html = re.sub(r'<table[^>]*>\s*</table>', '', simple_html, flags=re.IGNORECASE)
            simple_html = re.sub(r'<tr[^>]*>\s*</tr>', '', simple_html, flags=re.IGNORECASE)
            simple_html = re.sub(r'<td[^>]*>\s*</td>', '', simple_html, flags=re.IGNORECASE)
            simple_html = re.sub(r'<div[^>]*>\s*</div>', '', simple_html, flags=re.IGNORECASE)
            simple_html = re.sub(r'<span[^>]*>\s*</span>', '', simple_html, flags=re.IGNORECASE)
            # Remove width/height attributes that cause oversized layout
            simple_html = re.sub(r'\s(?:width|height)="[^"]*"', '', simple_html, flags=re.IGNORECASE)
            # Collapse excessive whitespace in HTML
            simple_html = re.sub(r'\n\s*\n', '\n', simple_html)

            # Wrap in clean container
            simple_html = f"""<div style="font-family:Arial,sans-serif;font-size:11px;color:#333">
                {simple_html}
            </div>"""

            tmp_path = os.path.join(tempfile.gettempdir(), f"gather_{os.getpid()}_{id(simple_html)}.pdf")

            story = fitz.Story(html=simple_html)
            writer = fitz.DocumentWriter(tmp_path)
            mediabox = fitz.paper_rect("letter")
            where = mediabox + (36, 36, -36, -36)

            page_count = 0
            more = True
            prev_filled = -1
            while more and page_count < 10:
                device = writer.begin_page(mediabox)
                more, filled = story.place(where)
                story.draw(device)
                writer.end_page()
                page_count += 1
                # Stop if Story is no longer placing new content
                if filled == prev_filled and page_count > 1:
                    break
                prev_filled = filled
            writer.close()
            del writer
            del story

            with open(tmp_path, 'rb') as f:
                data = f.read()

            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            # Validate not blank
            from core.gather.doc_scorer import extract_text
            check_text = extract_text(data, "check.pdf")
            if len(check_text) < 30:
                logger.debug("Generated PDF is blank — skipping")
                return None

            return data
        except Exception as e:
            logger.error("Failed to create email PDF: %s", e)
            return None

    # ── File saving ───────────────────────────────────────────────

    def _save_file(self, filename: str, data: bytes) -> Path:
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', filename)
        dest = self.output_dir / safe_name
        counter = 1
        while dest.exists():
            stem = Path(safe_name).stem
            suffix = Path(safe_name).suffix
            dest = self.output_dir / f"{stem}-{counter}{suffix}"
            counter += 1
        dest.write_bytes(data)
        return dest

    # ── Main gather ───────────────────────────────────────────────

    def gather(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        progress_callback=None,
    ) -> GatherResult:
        """Multi-pass gather from Gmail.

        Pass 1: Vendor emails with attachments (telecom, utilities, cloud providers, etc.)
        Pass 2: Sent folder — invoices emailed to clients
        Pass 3: Receipt/bill emails without attachments — save body as PDF
        Pass 4: Broad financial attachment search
        Pass 5: Broad body receipt search

        Args:
            since: Start of date range (default: 30 days ago)
            until: End of date range (default: None = now)
            progress_callback: Optional callable(phase, detail) for progress updates
        """
        result = GatherResult(source="gmail")

        if not self.is_connected():
            result.errors.append("Gmail not connected")
            return result

        if since is None:
            since = datetime.now() - timedelta(days=30)

        try:
            service = self._get_service()
        except Exception as e:
            result.errors.append(f"Failed to authenticate: {e}")
            return result

        watchlist = self._get_watchlist()
        date_range = self._date_range_query(since, until)

        if progress_callback:
            progress_callback("search", "Searching your inbox...")

        # Pass 1: Vendor emails with attachments
        try:
            q1 = self._build_vendor_query(watchlist, since, until)
            logger.info("Gmail Pass 1 (vendor attachments): %s", q1)
            msgs1 = self._list_messages(service, q1)
            logger.info("Pass 1: %d emails found", len(msgs1))
            if progress_callback:
                progress_callback("download", f"Downloading attachments from {len(msgs1)} vendor emails...")
            self._process_attachments(service, msgs1, result)
        except Exception as e:
            result.errors.append(f"Pass 1 error: {e}")
            logger.exception("Gmail Pass 1 failed")

        # Pass 2: Sent folder invoices
        try:
            q2 = self._build_sent_invoice_query(since, until)
            logger.info("Gmail Pass 2 (sent invoices): %s", q2)
            msgs2 = self._list_messages(service, q2)
            logger.info("Pass 2: %d emails found", len(msgs2))
            self._process_attachments(service, msgs2, result)
        except Exception as e:
            result.errors.append(f"Pass 2 error: {e}")
            logger.exception("Gmail Pass 2 failed")

        # Pass 3: Receipt emails without attachments → save body as PDF
        try:
            q3 = self._build_body_receipt_query(watchlist, since, until)
            logger.info("Gmail Pass 3 (body receipts): %s", q3)
            msgs3 = self._list_messages(service, q3)
            logger.info("Pass 3: %d emails found", len(msgs3))
            if progress_callback:
                progress_callback("score", f"Scoring {len(msgs3)} email bodies...")
            self._process_body_emails(service, msgs3, result)
        except Exception as e:
            result.errors.append(f"Pass 3 error: {e}")
            logger.exception("Gmail Pass 3 failed")

        # Pass 4: Broad financial attachment search (catches banks, vendors, etc.)
        try:
            q4 = (
                f"(subject:invoice OR subject:receipt OR subject:statement "
                f"OR subject:confirmation OR subject:bill OR subject:charge "
                f"OR subject:payment OR subject:wire OR subject:transfer) "
                f"has:attachment {date_range}"
            )
            logger.info("Gmail Pass 4 (broad financial attachments): %s", q4)
            msgs4 = self._list_messages(service, q4)
            logger.info("Pass 4: %d emails found", len(msgs4))
            if progress_callback:
                progress_callback("extract", f"Extracting from {len(msgs4)} financial emails...")
            self._process_attachments(service, msgs4, result)
        except Exception as e:
            result.errors.append(f"Pass 4 error: {e}")
            logger.exception("Gmail Pass 4 failed")

        # Pass 5: Body PDFs for financial emails not caught by Pass 3
        try:
            q5 = (
                f"(subject:confirmation OR subject:wire OR subject:transfer "
                f"OR subject:charge OR subject:payment "
                f"OR subject:receipt) "
                f"-has:attachment {date_range}"
            )
            logger.info("Gmail Pass 5 (broad body receipts): %s", q5)
            msgs5 = self._list_messages(service, q5)
            logger.info("Pass 5: %d emails found", len(msgs5))
            self._process_body_emails(service, msgs5, result)
        except Exception as e:
            result.errors.append(f"Pass 5 error: {e}")
            logger.exception("Gmail Pass 5 failed")

        # ── Rule 20: drop body PDFs that fail the substantive proof test ──
        try:
            self._apply_rule_20(result)
        except Exception as e:
            result.errors.append(f"Rule 20 error: {e}")
            logger.exception("Rule 20 failed")

        return result

    # ── Rule 20: Substantive proof test for email body PDFs ─────

    _DOLLAR_RE = re.compile(r'\$\s*\d[\d,]*\.?\d*|(?:CAD|USD|EUR|GBP)\s*\d[\d,]*\.?\d*', re.IGNORECASE)
    _COUNTERPARTY_RE = re.compile(
        r'\bto\s+([A-Z][A-Za-z0-9\s&.]+(?:Inc|LLC|Ltd|Corp|Co)?)'
        r'|(?:from|vendor|merchant|payee|paid\s+to)\s*[:\-]?\s*([A-Z][A-Za-z])'
        r'|your\s+([A-Z][A-Za-z\s]+?)\s+(?:bill|invoice|receipt|statement|order|account)'
        r'|(?:payment|charge)\s+(?:to|from)\s+([A-Z][A-Za-z])',
        re.MULTILINE,
    )
    _TXN_ID_RE = re.compile(
        r'(?:order|transaction|invoice|reference|confirmation|account)\s*(?:#|number|id|no)?[:\s]*([A-Z0-9\-]{4,})'
        r'|\b[A-Z]{2,4}[-]?\d{6,}\b'
        r'|\baccount\s*#?\s*\d{4,}',
        re.IGNORECASE,
    )

    def _substantive_proof_score(self, text: str) -> tuple[int, list[str]]:
        """Score email body text against the 2-of-3 substantive proof test.

        Returns (score 0-3, list of which criteria passed).
        """
        criteria = []

        # (a) Specific dollar amount in the body
        if self._DOLLAR_RE.search(text):
            criteria.append("amount")

        # (b) Named counterparty
        if self._COUNTERPARTY_RE.search(text):
            criteria.append("counterparty")

        # (c) Transaction identifier
        if self._TXN_ID_RE.search(text):
            criteria.append("txn_id")

        return len(criteria), criteria

    def _apply_rule_20(self, result: GatherResult) -> None:
        """Apply the 2-of-3 substantive proof test to all email body PDFs.

        Body PDFs with ≥2 of (amount, counterparty, txn_id) are kept.
        Body PDFs with 0-1 are removed as vague notifications.
        """
        import json as _json

        from core.gather.doc_scorer import extract_text

        cursor = self.db._conn.execute(
            """SELECT id, source_id, filename, stored_path, metadata_json
               FROM gather_log WHERE source = 'gmail' AND status = 'GATHERED'"""
        )
        logs = []
        for row in cursor.fetchall():
            meta = _json.loads(row[4]) if row[4] else {}
            logs.append({
                "id": row[0], "source_id": row[1], "filename": row[2],
                "stored_path": row[3], "meta": meta,
            })

        removed = 0
        kept = 0
        for entry in logs:
            meta = entry["meta"]
            if meta.get("type") != "body_pdf":
                continue  # Only check email body PDFs, not attachments

            stored = entry.get("stored_path", "")
            if not stored or not Path(stored).exists():
                continue

            # Extract text from the generated PDF
            text = extract_text(Path(stored).read_bytes(), entry["filename"])
            score, criteria = self._substantive_proof_score(text)

            if score >= 2:
                kept += 1
                logger.info("Rule 20 KEEP: %s (score %d/3: %s)", entry["filename"], score, criteria)
            else:
                # Remove — vague notification
                Path(stored).unlink()
                dl = _gather_mirror_dir() / "gmail" / Path(stored).name
                if dl.exists():
                    dl.unlink()
                removed += 1
                result.documents_new = max(0, result.documents_new - 1)
                logger.info("Rule 20 DROP: %s (score %d/3: %s)", entry["filename"], score, criteria)

        if removed > 0 or kept > 0:
            result.scores.append({
                "filename": "_rule_20_summary",
                "rule_20_removed": removed,
                "rule_20_kept": kept,
                "included": True,
            })
