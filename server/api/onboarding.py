"""Onboarding API — business profile, source configuration, OAuth flows.

- Auth via Depends(get_current_user)
- PostgreSQL via Depends(get_db)
- CloudCredentialStore for credential management
"""

from __future__ import annotations

import json
import logging
import os
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from db.database_pg import DatabasePg
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

# Per-route rate limiter for sensitive onboarding endpoints
limiter = Limiter(key_func=get_remote_address)


# ── Request Models ────────────────────────────────────────────────────

class BusinessProfileRequest(BaseModel):
    company_name: str | None = None
    fiscal_year: int | None = None
    base_currency: str = "CAD"
    tax_jurisdiction: str = "CA"
    province: str | None = None
    business_number: str | None = None
    industry: str | None = None
    business_summary: str | None = None
    naics_code: str | None = None

    @field_validator("naics_code")
    @classmethod
    def _validate_naics(cls, v):
        if v is None:
            return None  # field absent → upsert preserves the stored value
        v = v.strip()
        if v == "":
            # Explicit clear. Return "" (not None) so the COALESCE upsert
            # actually overwrites the stored code instead of preserving it —
            # the engine treats "" as "no NAICS" (missing-NAICS prompt).
            return ""
        if not re.fullmatch(r"\d{2,6}", v):
            raise ValueError("naics_code must be a 2–6 digit numeric string")
        return v


class SourceConfigRequest(BaseModel):
    enabled: bool = True
    config_json: dict | None = None


class GmailCallbackRequest(BaseModel):
    auth_code: str


class WiseTestRequest(BaseModel):
    token: str


class PayPalTestRequest(BaseModel):
    client_id: str
    client_secret: str


# ── Helper ───────────────────────────────────────────────────────────

def _get_creds(db: DatabasePg, user_id: str):
    from core.credentials_cloud import CloudCredentialStore
    return CloudCredentialStore(db, user_id)


def _user_has_existing_data(db: DatabasePg, user_id: str) -> bool:
    """Check if a user has any existing transactions or documents.

    An account that already holds data has clearly been used, so the wizard is
    skipped for it even when ``onboarding_complete`` is still false.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM transactions WHERE user_id = %s LIMIT 1
                   ) OR EXISTS(
                       SELECT 1 FROM documents WHERE user_id = %s LIMIT 1
                   )""",
                (user_id, user_id),
            )
            return cur.fetchone()[0]


def _connect_integration(db: DatabasePg, source: str) -> None:
    """Enable a gather source and mark connection_health as HEALTHY."""
    db.upsert_gather_source(source, enabled=True)
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO connection_health
                           (user_id, source, status, last_checked, last_success)
                       VALUES (%s, %s, 'HEALTHY', NOW(), NOW())
                       ON CONFLICT (user_id, source) DO UPDATE SET
                           status = 'HEALTHY',
                           last_checked = NOW(),
                           last_success = NOW(),
                           error_message = NULL,
                           updated_at = NOW()""",
                    (db.user_id, source),
                )
    except Exception:
        pass


def _disconnect_integration(db: DatabasePg, source: str) -> None:
    """Disable a gather source and mark connection_health as DISCONNECTED."""
    db.upsert_gather_source(source, enabled=False)
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO connection_health
                           (user_id, source, status, last_checked, error_message)
                       VALUES (%s, %s, 'DISCONNECTED', NOW(), 'Disconnected by user')
                       ON CONFLICT (user_id, source) DO UPDATE SET
                           status = 'DISCONNECTED',
                           last_checked = NOW(),
                           error_message = 'Disconnected by user',
                           updated_at = NOW()""",
                    (db.user_id, source),
                )
    except Exception:
        pass


# ── Endpoints ─────────────────────────────────────────���──────────────

@router.get("/status")
async def onboarding_status(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Check if onboarding is complete, with step-level detail."""
    profile = db.get_business_profile()

    # Fetch onboarding steps
    steps = _get_onboarding_steps(db)
    completed_count = sum(1 for s in steps if s["status"] == "COMPLETED")
    skipped_count = sum(1 for s in steps if s["status"] == "SKIPPED")
    total_steps = len(steps)

    is_complete = profile is not None and profile.get("onboarding_complete", False)

    # An account can hold data and still have onboarding_complete=false. Check
    # for existing transactions or documents — if either is present the wizard
    # has nothing to offer, so mark the profile complete and stop re-checking.
    if not is_complete:
        has_data = _user_has_existing_data(db, user.id)
        if has_data:
            is_complete = True
            # Auto-mark profile as onboarding complete so future checks are fast
            db.upsert_business_profile({"onboarding_complete": True})

    return {
        "complete": is_complete,
        "has_profile": profile is not None,
        "steps": steps,
        "completed_count": completed_count,
        "skipped_count": skipped_count,
        "total_steps": total_steps,
    }


@router.get("/config")
async def onboarding_config(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return full onboarding config for re-opening the wizard."""
    creds = _get_creds(db, user.id)

    profile = db.get_business_profile() or {}
    sources = db.get_gather_sources()

    source_map = {}
    for src in sources:
        source_map[src["source_type"]] = src

    gmail_connected = creds.retrieve("gmail_refresh_token") is not None
    gmail_config = source_map.get("gmail", {}).get("config_json") or {}

    wise_connected = creds.retrieve("wise_api_token") is not None

    return {
        "profile": profile,
        "sources": {
            "gmail": {
                "enabled": source_map.get("gmail", {}).get("enabled", False),
                "connected": gmail_connected,
                "vendor_watchlist": gmail_config.get("vendor_watchlist", []),
            },
            "wise": {
                "enabled": source_map.get("wise", {}).get("enabled", False),
                "connected": wise_connected,
            },
            "amazon": {
                "enabled": source_map.get("amazon", {}).get("enabled", False),
                "connected": source_map.get("amazon", {}).get("enabled", False),
            },
            "manual": {
                "enabled": source_map.get("manual", {}).get("enabled", True),
            },
        },
    }


@router.post("/profile")
async def save_profile(
    req: BusinessProfileRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Save business profile."""
    db.upsert_business_profile({
        "company_name": req.company_name,
        "fiscal_year": req.fiscal_year,
        "base_currency": req.base_currency,
        "tax_jurisdiction": req.tax_jurisdiction,
        "province": req.province,
        "business_number": req.business_number,
        "industry": req.industry,
        "business_summary": req.business_summary,
        "naics_code": req.naics_code,
    })
    return {"status": "ok"}


@router.get("/sources")
async def list_sources(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List configured sources with status."""
    return {"sources": db.get_gather_sources()}


@router.post("/source/{source_type}")
async def save_source(
    source_type: str,
    req: SourceConfigRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Save a source configuration."""
    valid_types = {"gmail", "wise", "paypal", "manual", "amazon"}
    if source_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid source type: {source_type}")
    db.upsert_gather_source(source_type, req.enabled, req.config_json)
    return {"status": "ok"}


@router.post("/complete")
async def complete_onboarding(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Mark onboarding as complete."""
    db.upsert_business_profile({"onboarding_complete": True})
    return {"status": "ok"}


# ── Gmail OAuth ──────────────────────────────────────────────────────

@router.post("/gmail/auth-url")
@limiter.limit("5/minute")
async def gmail_auth_url(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Generate Gmail OAuth URL using server-side redirect flow."""
    import secrets

    from core.gather.gmail import GmailGatherer

    creds = _get_creds(db, user.id)
    gatherer = GmailGatherer(creds, db, "gather")

    # Generate and store CSRF state
    state = secrets.token_urlsafe(32)
    db.upsert_gmail_oauth_state(state)

    url = gatherer.start_oauth_flow(state=state)
    if not url:
        raise HTTPException(
            status_code=400,
            detail="Google OAuth not configured — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET",
        )
    return {"auth_url": url}


@router.get("/gmail/callback")
@limiter.limit("10/minute")
async def gmail_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Handle Google OAuth redirect callback.

    This is a GET endpoint — Google redirects here after user consent.
    No JWT auth available (this is a browser redirect, not an API call).
    Returns HTML that sends postMessage to the popup opener window.
    """
    from fastapi.responses import HTMLResponse

    from core.gather.gmail import GmailGatherer
    from db.connection import get_pool

    frontend_origin = os.environ.get("FRONTEND_URL", "http://localhost:5173")

    def _html_response(success: bool, error_code: str = "") -> HTMLResponse:
        return HTMLResponse(f"""<!DOCTYPE html>
<html><body><script>
if (window.opener) {{
    window.opener.postMessage({{
        type: "gmail_oauth_complete",
        success: {str(success).lower()},
        error: "{error_code}"
    }}, "{frontend_origin}");
}}
window.close();
</script>
<p>{"Gmail connected successfully." if success else f"Connection failed: {error_code}"} This window will close automatically.</p>
</body></html>""")

    # Handle user denial
    if error:
        return _html_response(False, "USER_DENIED")

    if not code or not state:
        return _html_response(False, "MISSING_PARAMS")

    # Validate CSRF state and extract user_id
    pool = get_pool()
    user_id = DatabasePg.validate_gmail_oauth_state(pool, state)
    if not user_id:
        return _html_response(False, "STATE_MISMATCH")

    # Exchange code for tokens
    creds_obj = GmailGatherer.exchange_code_for_tokens(code)
    if not creds_obj:
        return _html_response(False, "TOKEN_EXCHANGE_FAILED")

    # Store credentials and enable Gmail source
    from core.credentials_cloud import CloudCredentialStore
    db = DatabasePg(pool, user_id)
    cloud_creds = CloudCredentialStore(db, user_id)
    cloud_creds.store("gmail_refresh_token", creds_obj.refresh_token)
    cloud_creds.store("gmail_access_token", creds_obj.token)
    if creds_obj.expiry:
        cloud_creds.store("gmail_token_expiry", creds_obj.expiry.isoformat())
    db.upsert_gather_source("gmail", enabled=True)

    # Record that this account now grants Gmail access.
    db.record_gmail_consent()

    # Fetch and store the connected Gmail email address
    try:
        from core.gather.gmail import GmailGatherer
        gatherer = GmailGatherer(cloud_creds, db, "gather")
        account_email = gatherer.get_connected_email()
        if account_email:
            db.update_gather_source_account_email("gmail", account_email)
    except Exception as e:
        logger.warning("Could not fetch Gmail account email: %s", e)

    # Update connection health to HEALTHY
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO connection_health
                           (user_id, source, status, last_checked, last_success)
                       VALUES (%s, 'gmail', 'HEALTHY', NOW(), NOW())
                       ON CONFLICT (user_id, source) DO UPDATE SET
                           status = 'HEALTHY',
                           last_checked = NOW(),
                           last_success = NOW(),
                           error_message = NULL,
                           updated_at = NOW()""",
                    (user_id,),
                )
    except Exception:
        pass

    return _html_response(True)


@router.delete("/gmail/disconnect")
async def gmail_disconnect(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Disconnect Gmail account — revoke Google token, clear credentials.

    Previously fetched documents remain (no historical data deletion).
    """
    creds = _get_creds(db, user.id)

    # Attempt to revoke the Google token
    refresh_token = creds.retrieve("gmail_refresh_token")
    if refresh_token:
        try:
            import requests
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        except Exception as e:
            logger.warning("Gmail token revocation failed (non-blocking): %s", e)

    # Clear stored credentials
    creds.delete("gmail_refresh_token")
    creds.delete("gmail_access_token")
    creds.delete("gmail_token_expiry")

    # Disable Gmail source
    db.upsert_gather_source("gmail", enabled=False)

    # Record the revoked Gmail grant for this account.
    db.revoke_gmail_consent()

    # Update connection health
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO connection_health
                           (user_id, source, status, last_checked, error_message)
                       VALUES (%s, 'gmail', 'DISCONNECTED', NOW(), 'Disconnected by user')
                       ON CONFLICT (user_id, source) DO UPDATE SET
                           status = 'DISCONNECTED',
                           last_checked = NOW(),
                           error_message = 'Disconnected by user',
                           updated_at = NOW()""",
                    (db.user_id,),
                )
    except Exception:
        pass

    return {"status": "ok"}


# ── Wise ─────────────────────────────────────────────────────────────

@router.post("/wise/test")
@limiter.limit("5/minute")
async def wise_test(
    request: Request,
    req: WiseTestRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Validate a Wise API token, store it, and mark Wise as connected."""
    if not req.token or not req.token.strip():
        raise HTTPException(status_code=400, detail="Token is required")

    clean_token = req.token.strip()

    try:
        import httpx

        from core.wise_api import get_profile_id
        profile_id = get_profile_id(clean_token)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403):
            raise HTTPException(status_code=400, detail="Wise rejected the token — check that it is correct and has read access")
        if status >= 500:
            raise HTTPException(status_code=503, detail="Wise is temporarily unavailable")
        raise HTTPException(status_code=400, detail=f"Wise API error ({status}) — try again or regenerate your token")
    except Exception as e:
        error_str = str(e).lower()
        if "timeout" in error_str or "connection" in error_str:
            raise HTTPException(status_code=503, detail="Wise is temporarily unavailable")
        raise HTTPException(status_code=400, detail="Failed to verify Wise token")

    if profile_id is None:
        raise HTTPException(status_code=400, detail="Invalid Wise token — no profiles found for this token")

    creds = _get_creds(db, user.id)
    creds.store("wise_api_token", clean_token)
    creds.store("wise_profile_id", str(profile_id))
    _connect_integration(db, "wise")

    return {"status": "ok", "profile_id": profile_id}


@router.delete("/wise/disconnect")
async def wise_disconnect(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Disconnect Wise account and delete stored token.

    Previously fetched documents remain (no historical data deletion).
    """
    creds = _get_creds(db, user.id)
    creds.delete("wise_api_token")
    _disconnect_integration(db, "wise")

    return {"status": "ok"}


# ── PayPal ──────────────────────────────────────────────────────────

@router.post("/paypal/test")
@limiter.limit("5/minute")
async def paypal_test(
    request: Request,
    req: PayPalTestRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Test PayPal credentials and store them if valid.

    Exchanges client_id + client_secret for an access token to verify validity.
    On success, stores credentials in CloudCredentialStore and enables PayPal source.
    """
    if not req.client_id or not req.client_id.strip():
        raise HTTPException(status_code=400, detail="Client ID is required")
    if not req.client_secret or not req.client_secret.strip():
        raise HTTPException(status_code=400, detail="Client Secret is required")

    try:
        from core.paypal_api import get_access_token
        token = get_access_token(req.client_id.strip(), req.client_secret.strip())
    except Exception as e:
        error_str = str(e).lower()
        if "timeout" in error_str or "connection" in error_str:
            raise HTTPException(status_code=503, detail="PayPal is temporarily unavailable")
        raise HTTPException(
            status_code=400,
            detail="PayPal credentials are invalid \u2014 check your Client ID and Secret",
        )

    if token is None:
        raise HTTPException(
            status_code=400,
            detail="PayPal credentials are invalid \u2014 check your Client ID and Secret",
        )

    # Store credentials
    creds = _get_creds(db, user.id)
    creds.store("paypal_client_id", req.client_id.strip())
    creds.store("paypal_client_secret", req.client_secret.strip())
    _connect_integration(db, "paypal")

    return {"status": "ok"}


@router.delete("/paypal/disconnect")
async def paypal_disconnect(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Disconnect PayPal and delete stored credentials.

    Previously fetched transactions remain (no historical data deletion).
    """
    creds = _get_creds(db, user.id)
    creds.store("paypal_client_id", "")
    creds.store("paypal_client_secret", "")
    _disconnect_integration(db, "paypal")

    return {"status": "ok"}


# ── Amazon ──────────────────────────────────────────────────────────

@router.post("/amazon/preview")
async def amazon_preview(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Preview an Amazon CSV file before importing.

    Returns format detection, order count, date range, and sample orders.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB limit
        raise HTTPException(status_code=400, detail="File is too large (max 10 MB)")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="File has encoding issues. Try opening in Excel, saving as UTF-8 CSV, and re-uploading.",
            )

    from core.amazon_parser import preview_csv
    result = preview_csv(text)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/amazon/import")
async def amazon_import(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Import Amazon orders from a CSV file upload.

    Accepts multipart/form-data with a CSV file. Parses orders, deduplicates
    against existing transactions, and inserts new ones.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File is too large (max 10 MB)")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="File has encoding issues. Try saving as UTF-8 CSV.",
            )

    from datetime import datetime as dt

    from core.amazon_parser import AmazonFormat, parse_csv

    source_file = f"amazon_import_{dt.now().strftime('%Y%m%d_%H%M%S')}"
    result = parse_csv(text, source_file)

    if result.format == AmazonFormat.UNKNOWN:
        raise HTTPException(
            status_code=400,
            detail="Unrecognized CSV format. Make sure you downloaded from Amazon Business Analytics or Amazon Privacy Central.",
        )

    if not result.transactions:
        if result.errors:
            raise HTTPException(status_code=400, detail=f"No orders could be parsed: {result.errors[0]}")
        raise HTTPException(status_code=400, detail="No orders found in this file")

    # Insert transactions, skip duplicates
    imported = 0
    skipped = 0
    for txn in result.transactions:
        try:
            db.insert_transaction(txn)
            imported += 1
        except Exception:
            skipped += 1  # Duplicate (source_row hash collision = same order)

    # Mark Amazon as connected
    if imported > 0:
        _connect_integration(db, "amazon")

    # Send SSE event
    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {
            "source": "amazon",
            "imported": imported,
            "skipped": skipped,
        })
    except Exception:
        pass

    return {
        "status": "ok",
        "imported": imported,
        "skipped": skipped,
        "errors": len(result.errors),
        "date_range": {
            "from": result.date_range[0].isoformat() if result.date_range[0] else None,
            "to": result.date_range[1].isoformat() if result.date_range[1] else None,
        },
    }


@router.delete("/amazon/disconnect")
async def amazon_disconnect(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Mark Amazon as disconnected.

    Does NOT delete previously imported transactions (historical data persists).
    """
    _disconnect_integration(db, "amazon")
    return {"status": "ok"}


# ── Onboarding Steps ───────────────────────────────────────────────────

ONBOARDING_STEPS = ["profile", "data_sources", "receipt_upload", "first_upload", "first_reconciliation", "export"]


class OnboardingStateRequest(BaseModel):
    """Request body for saving onboarding wizard state."""
    step: str
    state_json: dict | None = None


def _get_onboarding_steps(db: DatabasePg) -> list[dict]:
    """Return every onboarding step with its status.

    A step with no row yet is reported as PENDING rather than inserted, so the
    table only ever holds steps the user has actually reached.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT step, status, completed_at FROM onboarding_steps WHERE user_id = %s",
                (db.user_id,),
            )
            existing = {row[0]: {"step": row[0], "status": row[1], "completed_at": str(row[2]) if row[2] else None}
                        for row in cur.fetchall()}

            steps = []
            for step_name in ONBOARDING_STEPS:
                if step_name in existing:
                    steps.append(existing[step_name])
                else:
                    steps.append({"step": step_name, "status": "PENDING", "completed_at": None})
            return steps


@router.get("/state")
async def get_onboarding_state(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return saved onboarding wizard state.

    Returns the onboarding_state_json from business_profile plus the
    individual step statuses from onboarding_steps.
    """
    profile = db.get_business_profile()
    state_json = {}
    if profile and profile.get("config_json"):
        state_json = profile["config_json"].get("onboarding_state", {})
    elif profile:
        # Check the dedicated onboarding_state_json column
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT onboarding_state_json FROM business_profile WHERE user_id = %s",
                    (db.user_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    state_json = json.loads(row[0]) if isinstance(row[0], str) else row[0]

    steps = _get_onboarding_steps(db)

    return {
        "state": state_json,
        "steps": steps,
    }


@router.post("/state")
async def save_onboarding_state(
    req: OnboardingStateRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Save onboarding wizard step state.

    Upserts the step record in onboarding_steps and stores arbitrary
    wizard state in business_profile.onboarding_state_json.
    """
    if req.step not in ONBOARDING_STEPS:
        raise HTTPException(status_code=400, detail=f"Invalid step: {req.step}")

    with db._conn() as conn:
        with conn.cursor() as cur:
            # Upsert onboarding step as COMPLETED
            cur.execute(
                """INSERT INTO onboarding_steps (user_id, step, status, completed_at)
                   VALUES (%s, %s, 'COMPLETED', NOW())
                   ON CONFLICT (user_id, step) DO UPDATE SET
                       status = 'COMPLETED',
                       completed_at = NOW()""",
                (db.user_id, req.step),
            )

            # Store wizard state in business_profile
            if req.state_json is not None:
                cur.execute(
                    """UPDATE business_profile
                       SET onboarding_state_json = COALESCE(onboarding_state_json, '{}')::jsonb || %s::jsonb,
                           updated_at = NOW()
                       WHERE user_id = %s""",
                    (json.dumps({req.step: req.state_json}), db.user_id),
                )

    return {"status": "ok", "step": req.step}


@router.patch("/skip/{step}")
async def skip_onboarding_step(
    step: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Skip an onboarding step.

    Sets the step status to SKIPPED in onboarding_steps and logs the
    action in the audit_log.
    """
    if step not in ONBOARDING_STEPS:
        raise HTTPException(status_code=400, detail=f"Invalid step: {step}")

    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO onboarding_steps (user_id, step, status)
                   VALUES (%s, %s, 'SKIPPED')
                   ON CONFLICT (user_id, step) DO UPDATE SET
                       status = 'SKIPPED'""",
                (db.user_id, step),
            )

            # Audit log
            cur.execute(
                """INSERT INTO audit_log
                   (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                   VALUES (%s, 'onboarding', 0, 'SKIP_STEP', %s, 'SKIPPED', %s)""",
                (db.user_id, step, user.id),
            )

    return {"status": "ok", "step": step, "new_status": "SKIPPED"}


@router.get("/nudges")
async def onboarding_nudges(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return contextual nudges based on the user's data state.

    Checks what the user has done so far and suggests next actions:
    - Upload a receipt if none exist
    - Connect a bank if no transactions
    - Run reconciliation if both exist but unmatched
    - Export if reconciliation is done
    """
    nudges = []

    with db._conn() as conn:
        with conn.cursor() as cur:
            # Check document count
            cur.execute(
                "SELECT COUNT(*) FROM documents WHERE user_id = %s",
                (db.user_id,),
            )
            doc_count = cur.fetchone()[0]

            # Check transaction count
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = %s",
                (db.user_id,),
            )
            txn_count = cur.fetchone()[0]

            # Check matched count
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = %s AND status = 'MATCHED'",
                (db.user_id,),
            )
            matched_count = cur.fetchone()[0]

            # Check unmatched count
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = %s AND status = 'UNMATCHED'",
                (db.user_id,),
            )
            unmatched_count = cur.fetchone()[0]

    # No profile
    profile = db.get_business_profile()
    if not profile:
        nudges.append({
            "id": "setup_profile",
            "priority": 1,
            "message": "Set up your business profile to get started",
            "action": "/onboarding",
            "step": "profile",
        })

    # No receipts
    if doc_count == 0:
        nudges.append({
            "id": "upload_receipt",
            "priority": 2,
            "message": "Upload your first proof of transaction to begin tracking expenses",
            "action": "/receipts/upload",
            "step": "receipt_upload",
        })

    # No transactions
    if txn_count == 0:
        nudges.append({
            "id": "connect_bank",
            "priority": 2,
            "message": "Connect a bank account or upload a bank statement",
            "action": "/onboarding",
            "step": "first_upload",
        })

    # Both exist but nothing matched
    if doc_count > 0 and txn_count > 0 and matched_count == 0:
        nudges.append({
            "id": "run_reconciliation",
            "priority": 3,
            "message": f"You have {txn_count} transactions and {doc_count} proofs of transaction ready to reconcile",
            "action": "/reconciliation",
            "step": "first_reconciliation",
        })

    # Reconciliation done, suggest export
    if matched_count > 0 and unmatched_count == 0:
        nudges.append({
            "id": "export_report",
            "priority": 4,
            "message": "All transactions matched! Export your report",
            "action": "/reports",
            "step": "export",
        })
    elif matched_count > 0 and unmatched_count > 0:
        nudges.append({
            "id": "review_unmatched",
            "priority": 3,
            "message": f"{unmatched_count} transactions still need proofs of transaction",
            "action": "/transactions?filter=unmatched",
            "step": "receipt_upload",
        })

    return {"nudges": sorted(nudges, key=lambda n: n["priority"])}


@router.get("/connections-health")
async def onboarding_connections_health(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Per-source connection health for the onboarding wizard.

    Lighter-weight than /api/connections/health — only returns
    source name, enabled flag, connected flag, and status.
    """
    creds = _get_creds(db, user.id)
    sources_config = db.get_gather_sources()
    source_map = {s["source_type"]: s for s in sources_config}

    connections = []

    # Gmail
    gmail_enabled = source_map.get("gmail", {}).get("enabled", False)
    gmail_connected = creds.retrieve("gmail_refresh_token") is not None
    connections.append({
        "source": "gmail",
        "enabled": gmail_enabled,
        "connected": gmail_connected,
        "status": "HEALTHY" if gmail_connected else ("DISCONNECTED" if gmail_enabled else "UNKNOWN"),
    })

    # Wise
    wise_enabled = source_map.get("wise", {}).get("enabled", False)
    wise_connected = creds.retrieve("wise_api_token") is not None
    connections.append({
        "source": "wise",
        "enabled": wise_enabled,
        "connected": wise_connected,
        "status": "HEALTHY" if wise_connected else ("DISCONNECTED" if wise_enabled else "UNKNOWN"),
    })

    return {"connections": connections}
