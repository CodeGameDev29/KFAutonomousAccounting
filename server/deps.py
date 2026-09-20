"""FastAPI dependency injection — provides user-scoped services.

All protected routes should inject ``AuthUser`` and ``DatabasePg`` via these
dependencies so that every database query is automatically scoped to the
authenticated user.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import Depends

from db.connection import get_database
from db.database_pg import DatabasePg
from server.auth import AuthUser, get_current_user, get_current_user_header_or_query

logger = logging.getLogger(__name__)


async def get_db(user: AuthUser = Depends(get_current_user)) -> DatabasePg:
    """Return a user-scoped database instance.

    Usage: Add ``db: DatabasePg = Depends(get_db)`` to any protected route.
    The database automatically filters all queries to this user's data
    via PostgreSQL Row Level Security (RLS).
    """
    return get_database(user.id)


async def get_db_header_or_query(
    user: AuthUser = Depends(get_current_user_header_or_query),
) -> DatabasePg:
    """Like ``get_db`` but accepts auth via Authorization header OR ``?token=``.

    Use on endpoints triggered by direct browser navigation (``<a href>``)
    where custom headers cannot be set — e.g. file download endpoints that
    rely on Content-Disposition for the saved filename.
    """
    return get_database(user.id)


# ── Feature-flag helper ──────────────────────────────────────────────

def _parse_default_on_flags() -> frozenset[str]:
    raw = os.getenv("BETA_FLAGS_DEFAULT_ON", "[]")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ValueError("BETA_FLAGS_DEFAULT_ON must be a JSON array of strings")
        return frozenset(parsed)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("BETA_FLAGS_DEFAULT_ON parse failed (%s) — treating as empty", e)
        return frozenset()


# Flags that are ON by default for everyone at the code level (independent of
# the BETA_FLAGS_DEFAULT_ON env var, so they can't be accidentally unset). A
# user can still be explicitly turned OFF via business_profile.feature_flags.
_CODE_DEFAULT_ON_FLAGS: frozenset[str] = frozenset({"reasonableness_engine"})

_DEFAULT_ON_FLAGS: frozenset[str] = _parse_default_on_flags() | _CODE_DEFAULT_ON_FLAGS


def is_enabled(db: DatabasePg, user_id: str, flag_name: str) -> bool:
    """Public feature-flag check for beta gating.

    Authoritative: ``business_profile.feature_flags -> flag_name``.
    Fallback: ``BETA_FLAGS_DEFAULT_ON`` env var applies when the user
    has no ``business_profile`` row yet, or the flag key is absent/null.
    Fails closed (returns ``False``) on any DB error.
    """
    try:
        with db._conn() as conn:  # noqa: SLF001 — matches _has_feature_flag convention
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT feature_flags -> %s FROM business_profile WHERE user_id = %s",
                    (flag_name, user_id),
                )
                row = cur.fetchone()
    except Exception:
        logger.warning(
            "is_enabled DB error for user=%s flag=%s — defaulting False",
            user_id, flag_name,
        )
        return False

    if row is not None and row[0] is not None:
        return row[0] is True or row[0] == "true"

    # Row missing OR flag key absent/null → apply env-var default-on fallback.
    return flag_name in _DEFAULT_ON_FLAGS
