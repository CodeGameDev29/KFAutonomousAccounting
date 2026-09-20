"""Pre-signup email validation and server-side signup endpoints (public, no auth required).

Performs signup server-side against the local identity service
(``server/local_auth.py``). Sends confirmation emails via SMTP when
SMTP_HOST is configured, and otherwise logs the link — with the shipped
defaults signup auto-confirms instead (AUTH_AUTOCONFIRM=1).
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.rate_limit import rate_limit as _rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth-validation"])


# Allowlist of origins that may be used as the confirm redirect target.
# Kept in sync with CORS ALLOWED_ORIGINS in server/app.py — the raw Origin
# header is attacker-controlled, so it is echoed back only when it is listed.
_ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://localhost:3000,http://localhost:8080",
    ).split(",")
    if o.strip()
}
_DEFAULT_ORIGIN = os.environ.get("APP_URL") or "http://localhost:5174"

_IS_DEV = os.environ.get("APP_ENV", "production").lower() in {"development", "dev", "local"}


def _resolve_origin(request: Request) -> str:
    raw = request.headers.get("origin", "")
    return raw if raw in _ALLOWED_ORIGINS else _DEFAULT_ORIGIN


# Reuse the app-level slowapi Limiter if available for per-route rate limits.


class ValidateEmailRequest(BaseModel):
    email: str


class SignupRequest(BaseModel):
    email: str
    password: str


class ResetPasswordRequest(BaseModel):
    email: str


@router.post("/validate-email")
@_rate_limit("10/hour")
async def validate_email(body: ValidateEmailRequest, request: Request):
    """Check that an address is well-formed enough to sign up with.

    Kept as an endpoint so the sign-up form can validate before submitting.
    There is no domain blocklist: who may sign up is decided by
    AUTH_ALLOW_SIGNUP, not by guessing which mail providers are throwaway.

    Global rate limiter (60/min per IP) applies via app middleware.
    An additional per-route limit caps enumeration to 10/hour/IP.
    """
    email = body.email.strip().lower()
    if not email or "@" not in email:
        return JSONResponse(
            status_code=400,
            content={"valid": False, "reason": "invalid_email"},
        )

    return {"valid": True}


def _validate_password_strength(password: str) -> str | None:
    """Server-side password validation matching frontend rules."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Z]", password):
        return "Password must contain an uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must contain a lowercase letter."
    if not re.search(r"\d", password):
        return "Password must contain a number."
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?`~]", password):
        return "Password must contain a special character."
    return None


def _send_smtp_email(to_email: str, subject: str, text_body: str, html_body: str) -> bool:
    """Send a transactional email via SMTP.

    Requires SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM in .env.
    Returns True if sent successfully, False otherwise.
    """
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", user)

    if not host or not user or not password:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Autonomous Accounting <{from_addr}>"
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, to_email, msg.as_string())
        return True
    except Exception as e:
        logger.error("SMTP send failed for %s: %s", to_email, e)
        return False


def _send_confirmation_email(to_email: str, confirm_url: str) -> bool:
    """Send the signup confirmation email.

    Sends via SMTP when SMTP_HOST is configured. With no mail transport at all
    signups auto-confirm instead (AUTH_AUTOCONFIRM=1), in which case this
    returns False and nothing is sent.

    Returns True if delivery was attempted and accepted, False if SMTP is
    unconfigured or the send failed.
    """
    text_body = (
        f"Confirm your email for this Autonomous Accounting instance:\n\n"
        f"{confirm_url}\n\n"
        f"The link expires in 24 hours. If you did not create this account, "
        f"ignore this message; nothing happens without the link.\n"
    )

    html_body = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 32px 24px; color: #111;">
  <p style="color: #1a1a2e; font-size: 14px; line-height: 1.6; margin: 0 0 16px;">
    Confirm your email for this Autonomous Accounting instance:
  </p>
  <p style="margin: 0 0 20px;">
    <a href="{confirm_url}" style="color: #4338CA;">{confirm_url}</a>
  </p>
  <p style="color: #718096; font-size: 12px; line-height: 1.6; margin: 0;">
    The link expires in 24 hours. If you did not create this account, ignore
    this message; nothing happens without the link.
  </p>
</div>"""

    subject = "Confirm your Autonomous Accounting account"

    # SMTP, only if explicitly configured.
    if os.environ.get("SMTP_HOST"):
        return _send_smtp_email(to_email, subject, text_body, html_body)

    logger.info("confirmation email not sent (no SMTP_HOST) for %s", to_email)
    return False


def _send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """Send the password reset email via SMTP."""
    text_body = (
        f"A password reset was requested for your Autonomous Accounting "
        f"account.\n\n"
        f"Click the link below to choose a new password:\n\n"
        f"{reset_url}\n\n"
        f"This link expires in 1 hour.\n\n"
        f"If you didn't request a password reset, you can safely ignore this email — "
        f"your password will remain unchanged."
    )

    html_body = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 32px 24px;">
  <div style="text-align: center; margin-bottom: 32px;">
    <h1 style="font-size: 20px; font-weight: 600; color: #1a1a2e; margin: 0;">Autonomous Accounting</h1>
  </div>
  <h2 style="font-size: 18px; color: #1a1a2e; margin: 0 0 12px;">Reset your password</h2>
  <p style="color: #4a5568; font-size: 14px; line-height: 1.6; margin: 0 0 24px;">
    A password reset was requested for this account. Click the button below to choose a new password.
  </p>
  <div style="text-align: center; margin: 28px 0;">
    <a href="{reset_url}"
       style="display: inline-block; background: #4338CA; color: #fff; padding: 12px 32px;
              border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 14px;">
      Reset Password
    </a>
  </div>
  <p style="color: #718096; font-size: 12px; line-height: 1.5; margin: 24px 0 0;">
    This link expires in 1 hour. If you didn't request a password reset, ignore this email
    &mdash; your password will remain unchanged.
  </p>
</div>"""

    return _send_smtp_email(
        to_email,
        "Reset your Autonomous Accounting password",
        text_body,
        html_body,
    )




def signup_enabled() -> bool:
    """Whether new accounts may be created at all.

    Closed by default, deliberately: an open signup endpoint on a machine
    somebody runs themselves is an invitation to fill its disk and spend its
    model time. See the README for the posture this default assumes.

    Set AUTH_ALLOW_SIGNUP=1 to open it - a deliberate act, not a default.
    """
    return os.environ.get("AUTH_ALLOW_SIGNUP", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


@router.post("/signup")
@_rate_limit("5/hour")
async def server_signup(body: SignupRequest, request: Request):
    """Create a new account in the local user store.

    Refuses unless AUTH_ALLOW_SIGNUP is explicitly set; see signup_enabled().

    The account lives in ``auth.users`` in the server's own PostgreSQL
    cluster, so signup is a single local INSERT (see ``server/local_auth.py``)
    with no outbound request and no rate limit but the one this app applies.

    With ``AUTH_AUTOCONFIRM=1`` - the default, because there is no mail
    provider to deliver a link through until SMTP is configured - the account is
    usable immediately. Set it to 0 to require confirmation, in which case the
    link is emailed when SMTP is configured and written to the log when it is
    not.
    """
    if not signup_enabled():
        logger.info("Signup attempt while registration is closed")
        return JSONResponse(
            status_code=403,
            content={"error": "Autonomous Accounting is not accepting new accounts."},
        )

    email = body.email.strip().lower()
    password = body.password

    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"error": "Invalid email address."})

    pw_error = _validate_password_strength(password)
    if pw_error:
        return JSONResponse(status_code=400, content={"error": pw_error})

    from server import local_auth

    origin = _resolve_origin(request)

    try:
        user = local_auth.create_user(email, password)
    except ValueError:
        # Email-enumeration mitigation: return exactly the shape a fresh signup
        # returns. A real user owning this address already has their inbox -
        # they do not need this endpoint to confirm the account exists.
        logger.info("Signup attempt for existing email: %s", email)
        return {
            "success": True,
            "confirmed": False,
            "email_sent": False,
            "message": "Account created. Check your email for a confirmation link.",
        }
    except Exception as e:
        logger.error("Signup failed for %s: %s", email, e)
        return JSONResponse(
            status_code=500, content={"error": "Account creation failed. Please try again."}
        )

    confirmation_token = user.get("_confirmation_token")

    # Auto-confirmed: hand back a live session so the browser is signed in the
    # moment the form submits, with no second round trip.
    if not confirmation_token:
        logger.info("Signup complete (auto-confirmed): %s", email)
        session = local_auth.build_session(user)
        return {
            "success": True,
            "confirmed": True,
            "email_sent": False,
            "message": "Account created.",
            "session": session,
        }

    confirm_url = f"{origin}/auth/confirm?token={confirmation_token}"
    email_sent = _send_confirmation_email(email, confirm_url)

    result: dict = {
        "success": True,
        "confirmed": False,
        "email_sent": email_sent,
        "message": "Account created. Check your email for a confirmation link.",
    }

    if email_sent:
        logger.info("Confirmation email sent to %s via SMTP", email)
    else:
        # The link goes to the server log, never to the caller - see the
        # reasoning on reset-password below. A confirmation token is weaker
        # than a recovery token, but handing either to an anonymous requester
        # is the same mistake.
        logger.warning(
            "No mail transport configured - confirmation link for user_id=%s is "
            "available only in this log line: %s",
            user["id"], confirm_url,
        )

    return result


# Generic success response for the reset-password endpoint.
# Shared between the "user exists" and "user not found" branches so the
# response is indistinguishable - defeats email enumeration via this endpoint.
_RESET_GENERIC_SUCCESS = {
    "success": True,
    "message": "If an account exists for that email, a reset link has been sent.",
}


@router.post("/reset-password")
@_rate_limit("5/hour")
async def server_reset_password(body: ResetPasswordRequest, request: Request):
    """Mint a one-hour password-reset link for a local account.

    The response is identical whether or not the address has an account, so
    this endpoint cannot be used as an existence oracle.
    """
    email = body.email.strip().lower()

    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"error": "Invalid email address."})

    from server import local_auth

    origin = _resolve_origin(request)

    try:
        recovery_token = local_auth.create_recovery_token(email)
    except Exception as e:
        logger.error("Password reset failed for %s: %s", email, e)
        return JSONResponse(
            status_code=500, content={"error": "Failed to send reset link. Please try again."}
        )

    if not recovery_token:
        logger.info("Password reset requested for non-existent email: %s", email)
        return _RESET_GENERIC_SUCCESS

    reset_url = f"{origin}/auth/reset-confirm?token={urllib.parse.quote(recovery_token)}&type=recovery"

    email_sent = _send_password_reset_email(email, reset_url)
    if email_sent:
        logger.info("Password reset email sent to %s via SMTP", email)
        return _RESET_GENERIC_SUCCESS

    # NEVER put the reset link in the HTTP response.
    #
    # A recovery token is a full account-takeover credential: update_password
    # accepts it with no session at all. Returning it to whoever asked means
    # anyone on the internet who knows an address gets a one-hour takeover link
    # for that account. It also defeats this endpoint's own anti-enumeration
    # contract, because only a real address comes back with the extra field.
    #
    # With no mail transport configured, the recoverable path is the server
    # log, a file only whoever runs the server can read. That is the whole
    # reason the link goes to the log and not to the caller.
    logger.warning(
        "No mail transport configured - password reset link for %s is available "
        "only in this log line: %s",
        email,
        reset_url,
    )
    return _RESET_GENERIC_SUCCESS


@router.post("/resend")
@_rate_limit("5/hour")
async def resend_confirmation(body: ResetPasswordRequest, request: Request):
    """Re-send (or re-mint) a confirmation link for an unconfirmed address.

    Mirrors the reset endpoint's non-disclosure: the response never reveals
    whether the address exists or is already confirmed.
    """
    email = body.email.strip().lower()
    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"error": "Invalid email address."})

    from server import local_auth

    origin = _resolve_origin(request)
    generic = {"success": True, "message": "If that account needs confirming, a link has been sent."}

    try:
        token = local_auth.create_confirmation_token(email)
    except Exception as e:
        logger.error("confirmation resend failed for %s: %s", email, e)
        return JSONResponse(status_code=500, content={"error": "Failed to resend. Please try again."})

    if not token:
        return generic

    confirm_url = f"{origin}/auth/confirm?token={token}"
    if _send_confirmation_email(email, confirm_url):
        return generic

    # Log-only, for the same reason as the other two flows: returning it would
    # let any anonymous caller mint a confirmation link for an address they do
    # not control, and would turn this endpoint into an existence oracle.
    logger.warning(
        "No mail transport configured - confirmation link for %s is available "
        "only in this log line: %s",
        email, confirm_url,
    )
    return generic
