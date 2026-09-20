"""Connection health and reconnect API.

Provides endpoints for monitoring the health of all connected data sources
(Gmail, Wise, PayPal) and reconnecting when a source becomes degraded
or disconnected.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from db.database_pg import DatabasePg
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/connections", tags=["connections"])


def _get_creds(db: DatabasePg, user_id: str):
    from core.credentials_cloud import CloudCredentialStore
    return CloudCredentialStore(db, user_id)


@router.get("/health")
async def connections_health(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List connection health for all configured sources.

    Checks:
    - connection_health table for stored status
    - gather_sources table for enabled/disabled
    - credential store for token presence

    Returns a dict keyed by source name with status, last_checked, error info.
    """
    creds = _get_creds(db, user.id)
    sources_config = db.get_gather_sources()
    source_map = {s["source_type"]: s for s in sources_config}

    # Read stored health records
    health_records = {}
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT source, status, last_checked, last_success, error_message
                   FROM connection_health
                   WHERE user_id = %s""",
                (db.user_id,),
            )
            for row in cur.fetchall():
                health_records[row[0]] = {
                    "status": row[1],
                    "last_checked": str(row[2]) if row[2] else None,
                    "last_success": str(row[3]) if row[3] else None,
                    "error_message": row[4],
                }

    result = {}

    # Gmail
    gmail_cfg = source_map.get("gmail", {})
    gmail_has_token = creds.retrieve("gmail_refresh_token") is not None
    gmail_health = health_records.get("gmail", {})
    gmail_account_email = db.get_gather_source_account_email("gmail")
    result["gmail"] = {
        "source": "gmail",
        "enabled": gmail_cfg.get("enabled", False),
        "connected": gmail_has_token,
        "account_email": gmail_account_email,
        "status": gmail_health.get("status", "HEALTHY" if gmail_has_token else "DISCONNECTED"),
        "last_checked": gmail_health.get("last_checked"),
        "last_success": gmail_health.get("last_success"),
        "error_message": gmail_health.get("error_message"),
    }

    # Wise
    wise_cfg = source_map.get("wise", {})
    wise_has_token = creds.retrieve("wise_api_token") is not None
    wise_health = health_records.get("wise", {})
    result["wise"] = {
        "source": "wise",
        "enabled": wise_cfg.get("enabled", False),
        "connected": wise_has_token,
        "status": wise_health.get("status", "HEALTHY" if wise_has_token else "DISCONNECTED"),
        "last_checked": wise_health.get("last_checked"),
        "last_success": wise_health.get("last_success"),
        "error_message": wise_health.get("error_message"),
    }

    # Amazon — CSV upload integration (no stored credentials)
    amazon_cfg = source_map.get("amazon", {})
    amazon_health = health_records.get("amazon", {})
    amazon_connected = amazon_cfg.get("enabled", False)
    result["amazon"] = {
        "source": "amazon",
        "enabled": amazon_cfg.get("enabled", False),
        "connected": amazon_connected,
        "status": amazon_health.get("status", "HEALTHY" if amazon_connected else "DISCONNECTED"),
        "last_checked": amazon_health.get("last_checked"),
        "last_success": amazon_health.get("last_success"),
        "error_message": amazon_health.get("error_message"),
    }

    return {"connections": result}


@router.post("/{source}/reconnect")
async def reconnect_source(
    source: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Reconnect a data source.

    Behavior varies by source:
    - gmail: Returns a new OAuth URL for re-authorization
    - wise: Tests the stored token and reports status
    - paypal: Tests the configured credentials
    """
    valid_sources = {"gmail", "wise", "paypal", "amazon"}
    if source not in valid_sources:
        raise HTTPException(status_code=400, detail=f"Invalid source: {source}")

    creds = _get_creds(db, user.id)

    if source == "gmail":
        try:
            from core.gather.gmail import GmailGatherer
            gatherer = GmailGatherer(creds, db, "gather")
            url = gatherer.start_oauth_flow()
            if not url:
                raise HTTPException(
                    status_code=400,
                    detail="Google OAuth not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing)",
                )

            # Log reconnect action
            _log_audit(db, user.id, "connection", 0, "RECONNECT", source, "gmail_oauth_started")

            return {"action": "oauth", "auth_url": url}
        except HTTPException:
            raise
        except Exception as e:
            from server.errors import api_error
            raise api_error(500, "GMAIL_RECONNECT_FAILED",
                            "Gmail reconnect failed. Please try again or reauthorize.",
                            log_exc=e)

    elif source == "wise":
        token = creds.retrieve("wise_api_token")
        if not token:
            raise HTTPException(status_code=400, detail="No Wise token stored. Configure in onboarding.")

        try:
            from core.wise_api import get_profile_id
            profile_id = get_profile_id(token)
            if profile_id is None:
                _update_connection_health(db, user.id, "wise", "DISCONNECTED", "Token invalid")
                raise HTTPException(status_code=400, detail="Wise token is invalid or expired")

            _update_connection_health(db, user.id, "wise", "HEALTHY")
            _log_audit(db, user.id, "connection", 0, "RECONNECT", source, "wise_token_valid")

            return {"action": "tested", "status": "HEALTHY", "profile_id": profile_id}
        except HTTPException:
            raise
        except Exception as e:
            from server.errors import api_error
            _update_connection_health(db, user.id, "wise", "DEGRADED", str(e))
            raise api_error(500, "WISE_TEST_FAILED",
                            "Wise test failed. Please verify your Wise API token.",
                            log_exc=e)



def _update_connection_health(
    db: DatabasePg, user_id: str, source: str, status: str, error_message: str | None = None
) -> None:
    """Upsert connection_health record and send SSE event."""
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO connection_health
                           (user_id, source, status, last_checked, error_message)
                       VALUES (%s, %s, %s, NOW(), %s)
                       ON CONFLICT (user_id, source) DO UPDATE SET
                           status = EXCLUDED.status,
                           last_checked = NOW(),
                           last_success = CASE
                               WHEN EXCLUDED.status = 'HEALTHY' THEN NOW()
                               ELSE connection_health.last_success
                           END,
                           error_message = EXCLUDED.error_message,
                           updated_at = NOW()""",
                    (user_id, source, status, error_message),
                )
    except Exception:
        logger.exception("Failed to update connection_health for %s/%s", user_id, source)

    try:
        from server.api.events import send_event
        send_event(user_id, "connection_health_changed", {
            "source": source,
            "status": status,
            "error_message": error_message,
        })
    except Exception:
        pass


def _log_audit(
    db: DatabasePg, user_id: str,
    entity_type: str, entity_id: int,
    action: str, old_value: str | None = None, new_value: str | None = None,
) -> None:
    """Insert an audit log entry."""
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, entity_type, entity_id, action, old_value, new_value, user_id),
                )
    except Exception:
        logger.exception("Failed to insert audit log")
