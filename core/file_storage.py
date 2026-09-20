"""Local-disk file storage for receipts, statements and exports.

Files live on the server's own disk under ``STORAGE_ROOT``, laid out the way an
object store would key them, so a stored path is portable between the two:

    {STORAGE_ROOT}/{user_id}/{YYYYMM}/{content_hash_16}_{filename}
    {STORAGE_ROOT}/{user_id}/exports/{YYYYMMDD}-{filename}

The one thing an object store offers that a plain directory does not is the
signed URL - a link the browser can follow without an ``Authorization`` header,
which is what makes ``<img src>`` and ``window.location`` downloads work. That is
reproduced here with an HMAC over (path, expiry): ``get_download_url`` mints one
and ``verify_signed_url`` (used by ``server/api/files.py``) checks it.

Tenant isolation is by path prefix and is enforced by the callers, which pass
the authenticated ``user.id``; ``resolve_path`` additionally refuses any path
that escapes STORAGE_ROOT.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

logger = logging.getLogger("aa-storage")

# Default keeps everything inside the project directory when STORAGE_ROOT is
# unset, so the app starts with no storage configuration. .gitignore excludes
# data/.
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "storage"

SIGNED_URL_EXPIRY = int(os.environ.get("STORAGE_URL_TTL_SECONDS", "3600"))

# Import sites that reference a bucket name resolve it here; with a single
# local root there is only ever one "bucket".
BUCKET = "receipts"


def storage_root() -> Path:
    """Return the configured storage root, creating it on first use."""
    root = Path(os.environ.get("STORAGE_ROOT") or _DEFAULT_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _url_secret() -> bytes:
    """Secret the signed-URL HMAC is keyed on.

    Falls back to the JWT secret so a deployment that set only one of the two
    still produces unguessable links. An empty result would make every signed
    URL forgeable, so that is a hard failure rather than a warning.
    """
    secret = os.environ.get("STORAGE_URL_SECRET") or os.environ.get("AUTH_JWT_SECRET", "")
    if not secret:
        raise RuntimeError(
            "STORAGE_URL_SECRET (or AUTH_JWT_SECRET) must be set - "
            "without it, file download links cannot be signed."
        )
    return secret.encode("utf-8")


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename for safe use in storage paths."""
    filename = filename.replace("\x00", "")
    filename = filename.replace("/", "_").replace("\\", "_")
    filename = filename.replace("..", "_")
    filename = re.sub(r'[\x00-\x1f\x7f"\\]', "_", filename)
    filename = filename.replace(" ", "_")
    filename = re.sub(r"_+", "_", filename)
    # Reserved device names are a Windows-only footgun: CON.pdf cannot be
    # created there even inside a directory.
    stem = filename.split(".")[0].upper()
    if stem in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        filename = f"_{filename}"
    filename = filename[:200]
    return filename or "unnamed"


def resolve_path(path: str) -> Path:
    """Map a storage path to an absolute file path, refusing traversal.

    ``path`` is the value stored in the database (``user/month/hash_name``). Any
    attempt to climb out of the root - ``..`` segments, an absolute path, a
    drive letter - raises instead of reading an arbitrary file off the disk.

    An empty path is refused rather than resolved. ``root / ""`` is the storage
    root *directory*, so a document row whose ``stored_path`` is null would
    resolve to it and fail on ``read_bytes`` with a directory error - which the
    audit binder would report as "proof file not in this ZIP", a missing-file
    story for what is actually a missing-path row.
    """
    if not (path or "").strip():
        raise ValueError("empty storage path")
    root = storage_root()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes storage root: {path!r}")
    return candidate


def resolve_user_path(user_id: str, path: str) -> Path:
    """``resolve_path``, additionally confined to one user's own directory.

    A key that merely *starts with* ``{user_id}/`` is not proof of ownership:
    ``alpha/../beta/202601/x.pdf`` has the right prefix, stays inside the
    storage root, and resolves into another tenant's directory. Anything that
    acts on a key a client supplied goes through here, so the check is made on
    the resolved path rather than on the string.
    """
    if not (user_id or "").strip() or "/" in user_id or "\\" in user_id or user_id in {".", ".."}:
        raise ValueError(f"invalid storage owner: {user_id!r}")
    candidate = resolve_path(path)
    user_root = (storage_root() / user_id).resolve()
    if user_root not in candidate.parents:
        raise ValueError(f"path escapes the owner's directory: {path!r}")
    return candidate


# -- Signed URLs ------------------------------------------------------

def _sign(path: str, expires_at: int) -> str:
    mac = hmac.new(_url_secret(), f"{path}|{expires_at}".encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode("ascii").rstrip("=")


def verify_signed_url(path: str, expires_at: int, signature: str) -> bool:
    """Return True iff this server minted ``signature`` and the link is live."""
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(_sign(path, expires_at), signature)


def get_download_url(
    path: str,
    filename: str | None = None,
    expires_in: int = SIGNED_URL_EXPIRY,
) -> str:
    """Return a same-origin signed URL the browser can fetch without a token.

    Same contract as an object store's signed URL: time-limited, carries an
    optional download filename, and needs no Authorization header.
    """
    expires_at = int(time.time()) + int(expires_in)
    params = {"p": path, "e": str(expires_at), "s": _sign(path, expires_at)}
    if filename:
        safe_name = (
            filename.replace('"', "")
            .replace("\\", "")
            .replace("\n", "")
            .replace("\r", "")
        )
        params["d"] = safe_name
    return f"/api/files/blob?{urlencode(params, quote_via=quote)}"


def generate_presigned_url(path: str, expires_in: int = SIGNED_URL_EXPIRY) -> str:
    """Alias of :func:`get_download_url` for import sites that use this name."""
    return get_download_url(path, expires_in=expires_in)


# -- Reads and writes -------------------------------------------------

def upload_file(
    user_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str | None = None,
) -> str:
    """Write a file under the user's prefix, deduplicated by content hash.

    Returns the storage path (the value to persist in the database). An
    identical upload returns the existing path without rewriting the bytes,
    which is the re-upload behaviour ``server/api/receipts.py`` depends on.
    """
    if not content_type:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
    safe_filename = _sanitize_filename(filename)
    month = datetime.now(timezone.utc).strftime("%Y%m")
    path = f"{user_id}/{month}/{file_hash}_{safe_filename}"

    target = resolve_path(path)
    if target.exists() and target.stat().st_size == len(file_bytes):
        logger.info("Duplicate file, reusing existing: %s", path)
        return path

    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp name then replace, so a crash mid-write can never leave a
    # truncated receipt that later reads would treat as valid.
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(file_bytes)
    tmp.replace(target)
    logger.info("Stored %s (%d bytes)", path, len(file_bytes))
    return path


def upload_export(
    user_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str | None = None,
) -> str:
    """Write an export (PDF, XLSX, ZIP).

    Never deduplicated - exports are regenerated on each request and must
    overwrite the previous copy.
    """
    safe_filename = _sanitize_filename(filename)
    date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = f"{user_id}/exports/{date_prefix}-{safe_filename}"

    target = resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(file_bytes)
    tmp.replace(target)
    logger.info("Stored export %s (%d bytes)", path, len(file_bytes))
    return path


def download_file(path: str) -> bytes:
    """Read a stored file's bytes."""
    return resolve_path(path).read_bytes()


def delete_file(path: str) -> bool:
    """Delete a stored file. Returns True if it is gone afterwards."""
    try:
        target = resolve_path(path)
        target.unlink(missing_ok=True)
        logger.info("Deleted %s", path)
        return True
    except Exception as e:
        logger.error("Failed to delete %s: %s", path, e)
        return False


def get_upload_url(
    user_id: str,
    filename: str,
    content_type: str | None = None,
) -> dict:
    """Describe where the browser should send a direct upload.

    An object store would hand back a pre-signed PUT to the bucket. Here the API
    *is* the storage tier, so the destination is this app's own multipart
    endpoint. The response keeps the object-store shape so callers and the
    frontend need no change.
    """
    if not content_type:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe_filename = _sanitize_filename(filename)
    month = datetime.now(timezone.utc).strftime("%Y%m")
    path = f"{user_id}/{month}/{safe_filename}"
    return {
        "url": "/api/files/upload",
        "fields": {"Content-Type": content_type},
        "key": path,
        "token": "",
    }


def list_user_files(user_id: str, prefix: str = "") -> list[dict]:
    """List a user's stored files as dicts of key/name/size/updated_at."""
    root = storage_root()
    # A prefix is client-shaped input: "../other-user" must not list a neighbour.
    base = resolve_user_path(user_id, f"{user_id}/{prefix}") if prefix else resolve_path(user_id)
    if not base.exists():
        return []
    out: list[dict] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix == ".part":
            continue
        stat = p.stat()
        out.append(
            {
                "key": p.relative_to(root).as_posix(),
                "name": p.name,
                "size": stat.st_size,
                "updated_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return out


def validate_storage_connection() -> None:
    """Startup check: the storage root must exist and be writable.

    Called from server/app.py's lifespan and /health. Where an object store
    would verify the bucket and its credentials, the local equivalent is
    creating a file where receipts are about to go.
    """
    root = storage_root()
    probe = root / ".write-probe"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except Exception as e:
        raise RuntimeError(f"STORAGE_ROOT {root} is not writable: {e}") from e
    # Fail loudly at boot rather than on the first download attempt.
    _url_secret()
    logger.info("Local storage verified: %s", root)
