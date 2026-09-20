"""Gmail watcher — polls inbox for accounting-relevant emails.

Connects to Gmail via OAuth2, checks for new emails periodically,
uses an LLM to determine relevance, and downloads attachments when
available for the gather pipeline to process.

Setup:
1. Go to Google Cloud Console → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Desktop application)
3. Download the JSON and save as gmail_credentials.json in the project root
4. Run `python -m core.gmail_watcher --setup` to authorize (opens browser)
5. The app then polls Gmail automatically
"""

from __future__ import annotations

import base64
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Gmail API read-only scope — the app never sends or modifies emails
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "gmail_credentials.json"
TOKEN_FILE = PROJECT_ROOT / "gmail_token.json"


@dataclass
class EmailInfo:
    """Parsed email metadata."""
    message_id: str
    subject: str
    sender: str
    date: str
    body_snippet: str
    body_text: str
    attachments: list[dict]  # [{"filename": str, "mime_type": str, "attachment_id": str}]
    has_attachments: bool


def get_gmail_service():
    """Build an authenticated Gmail API service.

    Returns None if credentials are not set up yet.
    """
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        except Exception:
            logger.warning("Gmail token refresh failed. Re-authorization needed.")
            creds = None

    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            logger.warning(
                "Gmail not configured. Place gmail_credentials.json in project root "
                "and run: python -m core.gmail_watcher --setup"
            )
            return None

        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_emails(service, after_timestamp: str | None = None, max_results: int = 10) -> list[EmailInfo]:
    """Fetch recent emails from the inbox.

    Args:
        service: Gmail API service object.
        after_timestamp: Only fetch emails after this timestamp (epoch seconds as string).
        max_results: Maximum number of emails to return.

    Returns list of EmailInfo objects.
    """
    query = "in:inbox"
    if after_timestamp:
        query += f" after:{after_timestamp}"

    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for msg_meta in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_meta["id"], format="full"
        ).execute()

        headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
        subject = headers.get("subject", "(no subject)")
        sender = headers.get("from", "unknown")
        date_str = headers.get("date", "")

        # Extract body text
        body_text = _extract_body_text(msg["payload"])

        # Find attachments
        attachments = _find_attachments(msg["payload"])

        emails.append(EmailInfo(
            message_id=msg_meta["id"],
            subject=subject,
            sender=sender,
            date=date_str,
            body_snippet=msg.get("snippet", ""),
            body_text=body_text[:2000],  # keep the prompt inside the model's context
            attachments=attachments,
            has_attachments=len(attachments) > 0,
        ))

    return emails


def download_attachment(service, message_id: str, attachment_id: str, filename: str) -> Path | None:
    """Download an email attachment to a temp file.

    Returns the path to the downloaded file, or None on failure.
    """
    try:
        att = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()

        data = base64.urlsafe_b64decode(att["data"])

        suffix = Path(filename).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="gmail_") as f:
            f.write(data)
            return Path(f.name)

    except Exception as e:
        logger.error("Failed to download attachment %s: %s", filename, e)
        return None


def _extract_body_text(payload: dict) -> str:
    """Recursively extract plain text body from email payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    for part in parts:
        text = _extract_body_text(part)
        if text:
            return text

    return ""


def _find_attachments(payload: dict) -> list[dict]:
    """Recursively find all attachments in an email payload."""
    attachments = []

    if payload.get("filename") and payload.get("body", {}).get("attachmentId"):
        attachments.append({
            "filename": payload["filename"],
            "mime_type": payload.get("mimeType", "application/octet-stream"),
            "attachment_id": payload["body"]["attachmentId"],
        })

    for part in payload.get("parts", []):
        attachments.extend(_find_attachments(part))

    return attachments


def build_relevance_prompt(email: EmailInfo) -> str:
    """Build a prompt asking the model whether an email is accounting-relevant."""
    return (
        "You are an accounting assistant. Determine if the following email is relevant "
        "to business accounting (invoices, receipts, bills, payments, bank statements, "
        "tax documents, subscription renewals, etc.).\n\n"
        f"From: {email.sender}\n"
        f"Subject: {email.subject}\n"
        f"Date: {email.date}\n"
        f"Has attachments: {email.has_attachments}\n"
        f"Attachment names: {', '.join(a['filename'] for a in email.attachments) if email.attachments else 'none'}\n\n"
        f"Body preview:\n{email.body_snippet}\n\n"
        "Respond with JSON:\n"
        '{\n'
        '  "relevant": true/false,\n'
        '  "type": "receipt" | "invoice" | "bill" | "bank_statement" | "tax" | "payment_confirmation" | "subscription" | "other" | null,\n'
        '  "suggested_name": "string — a short descriptive name for filing, e.g. Telecom-Jan2026, Cloud-Monthly, or null if not relevant",\n'
        '  "reason": "string — brief explanation"\n'
        '}'
    )


# --- CLI for initial setup ---

if __name__ == "__main__":
    import sys

    if "--setup" in sys.argv:
        print("Setting up Gmail API access...")
        print(f"Looking for credentials at: {CREDENTIALS_FILE}")

        if not CREDENTIALS_FILE.exists():
            print(f"\nERROR: {CREDENTIALS_FILE} not found.")
            print("1. Go to https://console.cloud.google.com/apis/credentials")
            print("2. Create OAuth 2.0 Client ID (Desktop application)")
            print("3. Download JSON and save as gmail_credentials.json in project root")
            sys.exit(1)

        service = get_gmail_service()
        if service:
            profile = service.users().getProfile(userId="me").execute()
            print(f"\nSuccess! Connected to: {profile['emailAddress']}")
            print(f"Token saved to: {TOKEN_FILE}")
        else:
            print("Failed to authenticate.")
            sys.exit(1)
    else:
        print("Usage: python -m core.gmail_watcher --setup")
