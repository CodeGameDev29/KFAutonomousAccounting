"""The local identity service - signup, sign-in, refresh, confirmation, reset.

Identity is not delegated to a third party. This module owns ``auth.users`` and
``auth.refresh_tokens`` in the local PostgreSQL cluster (created by
``db/local_auth_schema.sql``) and mints the JWTs that ``server/auth.py``
verifies.

Two design choices are worth stating:

**Password storage.** PBKDF2-HMAC-SHA256 with a per-user random salt and a work
factor recorded inside the stored hash, so the factor can be raised later
without invalidating existing passwords. Standard library only - no extra
dependency to keep patched.

**Refresh-token rotation, with a grace window.** Every refresh mints a new
token and marks the presented one *rotated*. For ``_REFRESH_GRACE_SECONDS``
after that, presenting the old token again is idempotent: it hands back the
same successor it was already rotated into, plus a fresh access token. That is
what stops three tabs on a long bulk upload from logging each other out — they
all converge on one live pair instead of the losers of the race being refused.

Outside that window the rotation is still single-use, and reuse is treated as
theft: replaying a rotated token after the grace, or replaying one whose
successor has itself already been rotated, revokes the **whole token family**
(every token descended from that sign-in, the newest included) and answers 401.
So a stolen refresh token is useful for at most the grace window, and using it
costs both the thief and the account owner the entire session — which is the
signal this design is after. ``sign_out`` and ``set_password`` revoke every one
of a user's tokens the same way.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db.connection import get_pool
from server.auth import AuthUser, access_token_ttl, get_current_user, mint_access_token
from server.rate_limit import rate_limit as _rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# PBKDF2 work factor. Raising this only affects passwords set afterwards -
# existing hashes carry their own iteration count and keep verifying.
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_ALGO = "pbkdf2_sha256"

# How long a rotated refresh token keeps answering with its successor. Long
# enough to cover a tab that was mid-flight (or asleep in a background tab)
# when a sibling rotated the pair; short enough that a stolen token is close to
# worthless.
_REFRESH_GRACE_SECONDS = 60

# Values written into auth.refresh_tokens.revoked_reason.
REVOKE_REASON_REUSE = "reuse_detected"
REVOKE_REASON_LOGOUT = "logout"
REVOKE_REASON_PASSWORD_CHANGE = "password_change"


# -- Password hashing -------------------------------------------------

def hash_password(password: str) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "$".join(
        [
            _PBKDF2_ALGO,
            str(_PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(dk).decode("ascii"),
        ]
    )


def is_legacy_bcrypt(stored: str | None) -> bool:
    """True for a stored hash in bcrypt format rather than PBKDF2."""
    return bool(stored) and stored[:4] in ("$2a$", "$2b$", "$2y$")


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verify against a stored hash. False on any malformed input.

    A NULL/empty ``stored`` means the account has no password at all — an
    account created through Google sign-in. Always False; such a user must set
    a password through the reset flow.

    Accepts two formats:

    * ``pbkdf2_sha256$...`` — what this module writes.
    * ``$2a$``/``$2b$``/``$2y$`` bcrypt — the format a password hash imported
      from another system may be in. Those keep verifying, and
      ``sign_in_with_password`` re-hashes to PBKDF2 on the next successful
      sign-in, so bcrypt rows drain on their own.
    """
    try:
        if is_legacy_bcrypt(stored):
            import bcrypt

            # bcrypt silently truncates at 72 bytes, so whatever wrote the
            # stored hash truncated the same way; match that here.
            return bcrypt.checkpw(
                password.encode("utf-8")[:72], stored.encode("utf-8")
            )

        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(salt_b64),
            int(iterations),
        )
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


def _upgrade_password_hash(user_id: str, password: str) -> None:
    """Re-hash a verified legacy password to PBKDF2. Never raises.

    Deliberately does NOT revoke sessions the way set_password does — the user
    just proved knowledge of this exact password, nothing has been compromised,
    and signing them out mid-login would be baffling.
    """
    try:
        pool = get_pool()
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE auth.users SET encrypted_password = %s, updated_at = NOW() WHERE id = %s",
                    (hash_password(password), user_id),
                )
            conn.commit()
            logger.info("Upgraded legacy password hash for user_id=%s", user_id)
        finally:
            pool.putconn(conn)
    except Exception as e:  # noqa: BLE001
        logger.warning("Password hash upgrade failed for %s: %s", user_id, e)


# -- User store -------------------------------------------------------

_USER_COLUMNS = (
    "id, email, encrypted_password, email_confirmed_at, confirmation_token, "
    "recovery_token, last_sign_in_at, raw_app_meta_data, raw_user_meta_data, "
    "is_active, created_at"
)


def _row_to_user(row: tuple) -> dict[str, Any]:
    """Map an auth.users row to the dict shape the frontend consumes."""
    (
        uid, email, _pw, email_confirmed_at, _ctok, _rtok,
        last_sign_in_at, app_meta, user_meta, is_active, created_at,
    ) = row
    return {
        "id": str(uid),
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "email_confirmed_at": email_confirmed_at.isoformat() if email_confirmed_at else None,
        "confirmed_at": email_confirmed_at.isoformat() if email_confirmed_at else None,
        "last_sign_in_at": last_sign_in_at.isoformat() if last_sign_in_at else None,
        "created_at": created_at.isoformat() if created_at else None,
        "app_metadata": app_meta or {"provider": "email", "providers": ["email"]},
        "user_metadata": {
            **(user_meta or {}),
            "email_verified": email_confirmed_at is not None,
        },
        "is_active": is_active,
        # Carried so the raw datetime is available to token minting without a
        # second query; stripped before the dict is serialised to the client.
        "_email_confirmed_at_dt": email_confirmed_at,
    }


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in user.items() if not k.startswith("_")}


def get_user_by_email(email: str) -> dict[str, Any] | None:
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_USER_COLUMNS} FROM auth.users WHERE LOWER(email) = LOWER(%s)",
                (email,),
            )
            row = cur.fetchone()
        return _row_to_user(row) if row else None
    finally:
        pool.putconn(conn)


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_USER_COLUMNS} FROM auth.users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return _row_to_user(row) if row else None
    finally:
        pool.putconn(conn)


def _password_hash_for(email: str) -> str | None:
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT encrypted_password FROM auth.users WHERE LOWER(email) = LOWER(%s)",
                (email,),
            )
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        pool.putconn(conn)


def autoconfirm_enabled() -> bool:
    """Whether a new signup is usable immediately.

    Defaults on. With no outbound mail transport configured there is nobody to
    deliver a confirmation link, so blocking on one would leave every new
    account unusable.
    """
    # Accept every spelling of "on". Comparing against "1" alone would make
    # AUTH_AUTOCONFIRM=true mean "off", the opposite of what it says.
    return os.environ.get("AUTH_AUTOCONFIRM", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def create_user(email: str, password: str, *, confirmed: bool | None = None) -> dict[str, Any]:
    """Create a user. Raises ValueError if the email is already registered.

    An existing *unconfirmed* account for the same address is replaced rather
    than rejected: otherwise anyone could squat an address they do not own and
    lock its real owner out of ever signing up. Confirmed accounts are never
    reclaimable this way.
    """
    email = email.strip().lower()
    confirmed = autoconfirm_enabled() if confirmed is None else confirmed
    now = datetime.now(timezone.utc)
    confirmation_token = None if confirmed else secrets.token_urlsafe(32)

    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email_confirmed_at FROM auth.users WHERE LOWER(email) = LOWER(%s)",
                (email,),
            )
            existing = cur.fetchone()
            if existing:
                if existing[1] is not None:
                    raise ValueError("already registered")
                cur.execute("DELETE FROM auth.users WHERE id = %s", (existing[0],))
                logger.info("Reclaimed unconfirmed account for %s (old_id=%s)", email, existing[0])

            cur.execute(
                """
                INSERT INTO auth.users
                    (email, encrypted_password, email_confirmed_at,
                     confirmation_token, confirmation_sent_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    email,
                    hash_password(password),
                    now if confirmed else None,
                    confirmation_token,
                    None if confirmed else now,
                ),
            )
            user_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

    logger.info("Created user %s (id=%s, confirmed=%s)", email, user_id, confirmed)
    user = get_user_by_id(str(user_id))
    assert user is not None
    user["_confirmation_token"] = confirmation_token
    return user


def set_password(user_id: str, password: str) -> None:
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.users
                   SET encrypted_password = %s, recovery_token = NULL, updated_at = NOW()
                 WHERE id = %s
                """,
                (hash_password(password), user_id),
            )
            # A password change invalidates every existing session - that is the
            # point of changing it after a suspected compromise. Every family of
            # this user's tokens dies, grace window included: `revoked` is
            # checked before anything else in rotate_refresh_token.
            cur.execute(
                """
                UPDATE auth.refresh_tokens
                   SET revoked = TRUE,
                       revoked_at = COALESCE(revoked_at, NOW()),
                       revoked_reason = COALESCE(revoked_reason, %s)
                 WHERE user_id = %s AND revoked = FALSE
                """,
                (REVOKE_REASON_PASSWORD_CHANGE, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# -- Sessions ---------------------------------------------------------

def _refresh_ttl_days() -> int:
    return int(os.environ.get("AUTH_REFRESH_TOKEN_TTL_DAYS", "30"))


def _new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def _as_utc(value: Any) -> datetime:
    """Coerce a stored timestamp to an aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# -- Refresh-token rotation -------------------------------------------
#
# The decision - rotate, replay, or declare theft - is a function over plain
# dicts and a small store, so the rules can be asserted without a PostgreSQL in
# the loop (``tests/test_refresh_token_rotation.py``). Same split as
# ``server/jobs/document_queue.py``: SQL in the store, judgement in a function.

class RefreshTokenStore(Protocol):
    """What rotation needs from wherever the tokens actually live."""

    def get(self, token: str) -> dict[str, Any] | None: ...

    def insert(
        self,
        *,
        token: str,
        user_id: str,
        family_id: str,
        generation: int,
        expires_at: datetime,
    ) -> None: ...

    def mark_rotated(self, token: str, *, replaced_by: str, rotated_at: datetime) -> bool: ...

    def revoke_family(self, family_id: str, *, reason: str, at: datetime) -> int: ...


@dataclass(frozen=True)
class RefreshDecision:
    """The outcome of presenting a refresh token.

    ``outcome`` is for the log, not the client: every failure answers the same
    401 so the endpoint cannot be used to probe which tokens ever existed.
    """

    outcome: str
    user_id: str | None = None
    refresh_token: str | None = None
    family_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.user_id is not None and self.refresh_token is not None


def rotate_refresh_token(
    store: RefreshTokenStore,
    token: str,
    *,
    now: datetime | None = None,
    ttl_days: int | None = None,
) -> RefreshDecision:
    """Rotate ``token``, replay its successor, or revoke its family.

    The rules, in the order they are checked:

    1. **Unknown, expired, or already revoked** → 401, and nothing is revoked.
       A token revoked by logout or a password change lands here too.
    2. **Never rotated** → mint the next generation in the same family, point
       this row at it, hand the new pair out. The ordinary path.
    3. **Rotated, but its successor has itself been rotated** → whoever holds
       this is two generations behind a live session, which no honest client
       ever is. Revoke the entire family and answer 401 - *even inside the
       grace window*.
    4. **Rotated within the last ``_REFRESH_GRACE_SECONDS``** → hand back the
       *same* successor it was rotated into. Idempotent: repeated presentations
       return the same token and mutate nothing, which is what lets several tabs
       that raced end up holding one live pair instead of one being signed out.
    5. **Rotated longer ago than that** → reuse. Revoke the family, answer 401.
    """
    now = now or datetime.now(timezone.utc)
    row = store.get(token)
    if row is None:
        return RefreshDecision("unknown")
    if row.get("revoked"):
        return RefreshDecision("revoked", family_id=_family_of(row))
    if _as_utc(row["expires_at"]) <= now:
        return RefreshDecision("expired", family_id=_family_of(row))

    if row.get("rotated_at") is None:
        return _rotate(store, row, now=now, ttl_days=ttl_days)
    return _replay(store, row, now=now)


def _family_of(row: dict[str, Any]) -> str:
    return str(row.get("family_id") or "")


def _rotate(
    store: RefreshTokenStore,
    row: dict[str, Any],
    *,
    now: datetime,
    ttl_days: int | None,
) -> RefreshDecision:
    """Mint the next generation of this family and retire the presented token."""
    user_id = str(row["user_id"])
    family_id = _family_of(row)
    successor = _new_refresh_token()
    ttl = _refresh_ttl_days() if ttl_days is None else ttl_days

    # Insert before marking, never after: a concurrent request that reads
    # ``replaced_by`` must always find the row it points at, or step 3 above
    # would read a missing successor as theft.
    store.insert(
        token=successor,
        user_id=user_id,
        family_id=family_id,
        generation=int(row.get("generation") or 0) + 1,
        expires_at=now + timedelta(days=ttl),
    )

    if not store.mark_rotated(row["token"], replaced_by=successor, rotated_at=now):
        # Two requests carrying the same token reached here within microseconds
        # and the other one won. Hand this caller the winner's successor so both
        # converge on one pair; ours was never transmitted to anybody.
        fresh = store.get(row["token"])
        winner = (fresh or {}).get("replaced_by")
        if winner:
            logger.info("Refresh race on family=%s; returning the winning successor", family_id)
            return RefreshDecision("replayed", user_id=user_id, refresh_token=str(winner), family_id=family_id)
        return RefreshDecision("rotated", user_id=user_id, refresh_token=successor, family_id=family_id)

    return RefreshDecision("rotated", user_id=user_id, refresh_token=successor, family_id=family_id)


def _replay(store: RefreshTokenStore, row: dict[str, Any], *, now: datetime) -> RefreshDecision:
    """Decide what a second presentation of an already-rotated token means."""
    user_id = str(row["user_id"])
    family_id = _family_of(row)
    successor_token = row.get("replaced_by")
    successor = store.get(str(successor_token)) if successor_token else None

    def _theft(why: str) -> RefreshDecision:
        revoked = store.revoke_family(family_id, reason=REVOKE_REASON_REUSE, at=now)
        logger.warning(
            "Refresh-token reuse (%s) for user_id=%s: revoked %d token(s) in family=%s",
            why, user_id, revoked, family_id,
        )
        return RefreshDecision("reuse_detected", family_id=family_id)

    if successor is None:
        # The row this was rotated into is gone. Nothing here can prove the
        # presenter is a live tab, so it is treated as reuse.
        return _theft("successor missing")
    if successor.get("rotated_at") is not None:
        return _theft("two generations behind")
    if successor.get("revoked"):
        # The family is already dead (logout, password change). Nothing to do.
        return RefreshDecision("revoked", family_id=family_id)
    if (now - _as_utc(row["rotated_at"])).total_seconds() > _REFRESH_GRACE_SECONDS:
        return _theft("grace expired")

    return RefreshDecision(
        "replayed", user_id=user_id, refresh_token=str(successor_token), family_id=family_id
    )


class PgRefreshTokenStore:
    """``auth.refresh_tokens``, as the handful of statements rotation needs."""

    _COLUMNS = (
        "token, user_id, family_id, generation, revoked, revoked_reason, "
        "revoked_at, rotated_at, replaced_by, expires_at, created_at"
    )

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        pool = get_pool()
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            pool.putconn(conn)

    def get(self, token: str) -> dict[str, Any] | None:
        import psycopg2.extras

        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"SELECT {self._COLUMNS} FROM auth.refresh_tokens WHERE token = %s",
                    (token,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def insert(
        self,
        *,
        token: str,
        user_id: str,
        family_id: str,
        generation: int,
        expires_at: datetime,
    ) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO auth.refresh_tokens
                        (token, user_id, family_id, generation, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (token, user_id, family_id, generation, expires_at),
                )

    def mark_rotated(self, token: str, *, replaced_by: str, rotated_at: datetime) -> bool:
        """Retire a token. False if somebody else already retired it."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth.refresh_tokens
                       SET rotated_at = %s, replaced_by = %s
                     WHERE token = %s AND rotated_at IS NULL
                    """,
                    (rotated_at, replaced_by, token),
                )
                return cur.rowcount > 0

    def revoke_family(self, family_id: str, *, reason: str, at: datetime) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth.refresh_tokens
                       SET revoked = TRUE,
                           revoked_at = COALESCE(revoked_at, %s),
                           revoked_reason = COALESCE(revoked_reason, %s)
                     WHERE family_id = %s AND revoked = FALSE
                    """,
                    (at, reason, family_id),
                )
                return cur.rowcount

    def prune_expired(self) -> None:
        """Opportunistic sweep so the table cannot grow without bound when
        nobody prunes it. Called on sign-in, which is rare and cheap."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM auth.refresh_tokens WHERE expires_at < NOW() - INTERVAL '7 days'"
                )


def _refresh_store() -> RefreshTokenStore:
    """The store the routes use. A seam the tests replace with a fake."""
    return PgRefreshTokenStore()


def _issue_refresh_token(user_id: str) -> str:
    """Start a brand-new token family: sign-in, signup, confirm, Google."""
    token = _new_refresh_token()
    store = PgRefreshTokenStore()
    store.insert(
        token=token,
        user_id=user_id,
        family_id=str(uuid.uuid4()),
        generation=0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=_refresh_ttl_days()),
    )
    store.prune_expired()
    return token


def mark_google_signin(user_id: str) -> None:
    """Record a successful Google sign-in.

    Also confirms the address: Google only hands us an ``email_verified`` claim
    for an address it has verified, and the callback refuses anything else, so
    reaching here is stronger proof of ownership than clicking an emailed link.
    That matters because an account created through Google may have no password
    to prove anything else with.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.users
                   SET last_sign_in_at = NOW(),
                       email_confirmed_at = COALESCE(email_confirmed_at, NOW()),
                       raw_app_meta_data = jsonb_set(
                           COALESCE(raw_app_meta_data, '{}'::jsonb),
                           '{provider}', '"google"'::jsonb, TRUE),
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (user_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Failed to record Google sign-in for %s", user_id)
    finally:
        pool.putconn(conn)


def _touch_last_sign_in(user_id: str) -> None:
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE auth.users SET last_sign_in_at = NOW() WHERE id = %s", (user_id,)
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        pool.putconn(conn)


def build_session(user: dict[str, Any], *, refresh_token: str | None = None) -> dict[str, Any]:
    """Mint an access + refresh token pair and shape it as a session object.

    Called with no ``refresh_token`` - sign-in, signup, confirm, the Google
    exchange - it starts a **new token family**. ``/api/auth/refresh`` passes the
    token rotation already decided on instead, so a replay inside the grace
    window hands back the same refresh token with a fresh access token rather
    than opening a second family per tab.
    """
    access_token, expires_at = mint_access_token(
        user_id=user["id"],
        email=user["email"],
        email_confirmed_at=user.get("_email_confirmed_at_dt"),
    )
    if refresh_token is None:
        refresh_token = _issue_refresh_token(user["id"])
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": access_token_ttl(),
        "expires_at": expires_at,
        "refresh_token": refresh_token,
        "user": _public_user(user),
    }


# -- Request models ---------------------------------------------------

class PasswordGrant(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ConfirmRequest(BaseModel):
    token: str


class UpdatePasswordRequest(BaseModel):
    password: str
    token: str | None = None  # recovery token, when not signed in


# -- Routes -----------------------------------------------------------

@router.post("/token")
@_rate_limit("20/minute")
async def sign_in_with_password(body: PasswordGrant, request: Request):
    """Email + password sign-in. Returns a session on success.

    Failures are deliberately indistinguishable - wrong password, unknown
    address and deactivated account all return the same 400 - so this endpoint
    cannot be used to enumerate which addresses have accounts.
    """
    email = body.email.strip().lower()
    generic = JSONResponse(
        status_code=400, content={"error": "Invalid login credentials."}
    )

    stored_hash = _password_hash_for(email)
    if not stored_hash:
        # Spend comparable time on the unknown-address path so response latency
        # does not become the oracle the equal error messages just closed.
        hash_password(body.password)
        return generic

    if not verify_password(body.password, stored_hash):
        return generic

    user = get_user_by_email(email)
    if not user or not user["is_active"]:
        return generic

    # An imported account may still carry a bcrypt hash. Now that the password
    # is verified, quietly move it to PBKDF2.
    if is_legacy_bcrypt(stored_hash):
        _upgrade_password_hash(user["id"], body.password)

    _touch_last_sign_in(user["id"])
    user = get_user_by_id(user["id"])
    assert user is not None
    logger.info("Sign-in: %s", email)
    return build_session(user)


@router.post("/refresh")
async def refresh_session(body: RefreshRequest):
    """Exchange a refresh token for a new session, rotating the token.

    Idempotent for ``_REFRESH_GRACE_SECONDS`` after a rotation, so the second
    and third tab of a long bulk upload get the same live pair rather than a 401
    that signs the whole browser out. Reuse outside that window kills the
    family - see ``rotate_refresh_token``.
    """
    decision = rotate_refresh_token(_refresh_store(), body.refresh_token)
    if not decision.ok:
        return JSONResponse(
            status_code=401, content={"error": "Invalid or expired refresh token."}
        )
    assert decision.user_id is not None  # implied by decision.ok
    user = get_user_by_id(decision.user_id)
    if not user or not user["is_active"]:
        return JSONResponse(status_code=401, content={"error": "Account unavailable."})
    return build_session(user, refresh_token=decision.refresh_token)


@router.post("/logout")
async def sign_out(body: RefreshRequest | None = None, user: AuthUser = Depends(get_current_user)):
    """Revoke this user's refresh tokens. Access tokens expire on their own.

    Every family, not just the one that carried this request: a sign-out on a
    shared machine has to end the session everywhere. ``revoked`` outranks the
    grace window, so a rotated-but-still-in-grace token stops working too.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.refresh_tokens
                   SET revoked = TRUE,
                       revoked_at = COALESCE(revoked_at, NOW()),
                       revoked_reason = COALESCE(revoked_reason, %s)
                 WHERE user_id = %s AND revoked = FALSE
                """,
                (REVOKE_REASON_LOGOUT, user.id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        pool.putconn(conn)
    return {"success": True}


@router.get("/user")
async def current_user(user: AuthUser = Depends(get_current_user)):
    """Return the signed-in user's current record, read fresh from the database.

    The frontend polls this to notice email confirmation, so it must reflect the
    stored row rather than replaying stale claims out of the presented token.
    """
    record = get_user_by_id(user.id)
    if not record:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user": _public_user(record)}


@router.post("/confirm")
async def confirm_email(body: ConfirmRequest):
    """Confirm an address with the token from the confirmation link."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.users
                   SET email_confirmed_at = COALESCE(email_confirmed_at, NOW()),
                       confirmation_token = NULL,
                       updated_at = NOW()
                 WHERE confirmation_token = %s
                 RETURNING id
                """,
                (body.token,),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

    if not row:
        return JSONResponse(
            status_code=400, content={"error": "This confirmation link is invalid or already used."}
        )
    user = get_user_by_id(str(row[0]))
    assert user is not None
    return build_session(user)


@router.post("/update-password")
@_rate_limit("10/hour")
async def update_password(body: UpdatePasswordRequest, request: Request):
    """Set a new password, authenticated either by a bearer token or a
    one-time recovery token from the reset email."""
    from server.api.auth_validation import _validate_password_strength

    pw_error = _validate_password_strength(body.password)
    if pw_error:
        return JSONResponse(status_code=400, content={"error": pw_error})

    user_id: str | None = None

    if body.token:
        pool = get_pool()
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM auth.users
                     WHERE recovery_token = %s
                       AND recovery_sent_at > NOW() - INTERVAL '1 hour'
                    """,
                    (body.token,),
                )
                row = cur.fetchone()
            user_id = str(row[0]) if row else None
        finally:
            pool.putconn(conn)
        if not user_id:
            return JSONResponse(
                status_code=400,
                content={"error": "This reset link is invalid or has expired."},
            )
    else:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(status_code=401, content={"error": "Not authenticated."})
        from fastapi.security import HTTPAuthorizationCredentials
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth_header[7:])
        current = await get_current_user(creds)
        user_id = current.id

    set_password(user_id, body.password)
    logger.info("Password updated for user_id=%s", user_id)
    return {"success": True}


def create_recovery_token(email: str) -> str | None:
    """Mint a one-hour recovery token, or None if no such account exists."""
    token = secrets.token_urlsafe(32)
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.users
                   SET recovery_token = %s, recovery_sent_at = NOW(), updated_at = NOW()
                 WHERE LOWER(email) = LOWER(%s)
                 RETURNING id
                """,
                (token, email),
            )
            row = cur.fetchone()
        conn.commit()
        return token if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def create_confirmation_token(email: str) -> str | None:
    """Re-mint a confirmation token for an unconfirmed address."""
    token = secrets.token_urlsafe(32)
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.users
                   SET confirmation_token = %s, confirmation_sent_at = NOW()
                 WHERE LOWER(email) = LOWER(%s) AND email_confirmed_at IS NULL
                 RETURNING id
                """,
                (token, email),
            )
            row = cur.fetchone()
        conn.commit()
        return token if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
