"""File upload/download API endpoints."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from config.settings import MAX_UPLOAD_BYTES, MAX_UPLOAD_MB
from core import file_storage
from db.database_pg import DatabasePg
from server import tickets
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["files"])

ALLOWED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp",
    ".gif", ".bmp", ".tiff", ".tif",
    ".xlsx", ".xls", ".csv",
    ".docx", ".doc",
}


class UploadURLRequest(BaseModel):
    filename: str
    content_type: str | None = None


class UploadURLResponse(BaseModel):
    url: str
    fields: dict
    key: str


class DownloadURLResponse(BaseModel):
    url: str


@router.post("/ticket")
async def mint_download_ticket(user: AuthUser = Depends(get_current_user)):
    """Mint a 60-second single-use ticket for authenticated file downloads.

    Used by report PDF/binder download endpoints and any other endpoint that
    requires a direct browser navigation (where Authorization headers cannot
    be set). Client flow:

        POST /api/files/ticket  (Authorization: Bearer <JWT>)
        → { "ticket": "abc…", "ttl_seconds": 60 }
        navigate: /api/reports/2026/4/pdf?ticket=abc…

    Preferred over passing a raw JWT as ``?token=``, which puts a credential
    in a URL.
    """
    token = tickets.issue(
        user.id,
        "file_download",
        ttl_seconds=60,
        email=user.email,
        provider=user.provider,
        email_verified=user.email_verified,
    )
    return {"ticket": token, "ttl_seconds": 60}


@router.post("/upload-url", response_model=UploadURLResponse)
async def get_upload_url(
    req: UploadURLRequest,
    user: AuthUser = Depends(get_current_user),
):
    """Get a signed URL for direct browser upload."""
    result = file_storage.get_upload_url(user.id, req.filename, req.content_type)
    return UploadURLResponse(**result)


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Upload a file through the server (alternative to signed upload URL).

    Use this for smaller files or when signed URL isn't practical.
    The file is uploaded to the file store and a document record is created.
    """
    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    file_bytes = await file.read()
    # One cap for every upload route — see config.settings.MAX_UPLOAD_MB.
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB}MB)"
        )
    content_type = file.content_type

    # Upload to the file store
    key = file_storage.upload_file(user.id, file_bytes, filename, content_type)

    return {
        "status": "ok",
        "key": key,
        "filename": filename,
        "size": len(file_bytes),
    }


@router.get("/download")
async def download_file(
    key: str,
    user: AuthUser = Depends(get_current_user),
):
    """Get a signed download URL for a file.

    Security: verifies the file belongs to the authenticated user by checking
    the storage path prefix matches the user's ID.
    """
    # Security check: ensure the key belongs to this user
    expected_prefix = f"{user.id}/"
    if not key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="Access denied")

    # Extract filename from key
    filename = key.rsplit("/", 1)[-1]
    # Remove hash prefix if present (format: {hash}_{filename})
    if "_" in filename and len(filename.split("_")[0]) == 16:
        filename = "_".join(filename.split("_")[1:])

    url = file_storage.get_download_url(key, filename)
    return DownloadURLResponse(url=url)


@router.delete("/{key:path}")
async def delete_file(
    key: str,
    user: AuthUser = Depends(get_current_user),
):
    """Delete a file from storage."""
    expected_prefix = f"{user.id}/"
    if not key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="Access denied")

    success = file_storage.delete_file(key)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete file")
    return {"status": "ok"}


@router.get("/blob")
async def serve_signed_blob(p: str, e: int, s: str, d: str | None = None):
    """Serve a stored file to a browser that presents a valid signed URL.

    This is the local equivalent of an object store's signed-URL endpoint, and
    it exists for the same reason: a browser following an ``<img src>`` or a
    ``window.location`` download cannot attach an Authorization header, so the
    capability has to travel in the URL itself.

    Authorisation is the HMAC alone - deliberately, since that is what makes the
    link usable without a session - so it is scoped tightly: the signature
    covers the exact path and expiry, and ``file_storage.resolve_path`` refuses
    anything that escapes the storage root. Links are short-lived
    (STORAGE_URL_TTL_SECONDS, default 1h) and minted only by endpoints that have
    already checked the caller owns the file.
    """
    if not file_storage.verify_signed_url(p, e, s):
        # One message for a bad signature and an expired link alike: telling
        # them apart would let someone probe for valid paths.
        raise HTTPException(status_code=403, detail="Invalid or expired link")

    try:
        target = file_storage.resolve_path(p)
    except ValueError:
        logger.warning("Rejected traversal attempt in signed blob request")
        raise HTTPException(status_code=403, detail="Invalid or expired link")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    import mimetypes

    from fastapi.responses import FileResponse

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = {}
    if d:
        # Quote-strip already happened when the URL was minted; re-strip anyway
        # so a hand-crafted link cannot inject header syntax.
        safe = d.replace('"', "").replace("\r", "").replace("\n", "")
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return FileResponse(target, media_type=media_type, headers=headers)
