"""Shared helper for classifying extraction errors into structured responses."""

from __future__ import annotations

import asyncio

from core.llm_errors import (
    LOCAL_LLM_UNREACHABLE_CODE,
    LOCAL_LLM_UNREACHABLE_MESSAGE,
    is_local_endpoint_down,
)

# An unreadable file, by the names the libraries that actually open it use.
#
# PyMuPDF raises ``pymupdf.FileDataError`` ("Failed to open file '...'") for a
# PDF whose body is damaged, truncated past repair or not a PDF at all, and
# ``EmptyFileError`` for a zero-byte one. Both are RuntimeError subclasses whose
# message carries no reliable keyword, so matching on the exception type is what
# makes the verdict dependable; without it an unreadable file classifies as a
# generic EXTRACTION_ERROR, which reads as "try again" and is retried. The
# message markers below catch the same conditions raised from pdfminer (the
# text-density path), Pillow (a corrupt image) and the receipt planner itself.
_CORRUPT_EXC_NAMES = frozenset({
    "FileDataError",          # pymupdf: damaged / not-a-PDF body
    "EmptyFileError",         # pymupdf: zero bytes
    "FzErrorFormat",          # mupdf, surfaced directly
    "PDFSyntaxError",         # pdfminer, via pdfplumber (text density)
    "PSSyntaxError",          # pdfminer lexer
    "PDFTextExtractionNotAllowed",
    "UnidentifiedImageError",  # Pillow: bytes that are not an image
})

_CORRUPT_MESSAGE_MARKERS = (
    "failed to open file",
    "cannot open",              # "cannot open empty file" / "broken document"
    "no objects found",
    "format error",
    "invalid pdf",
    "not a pdf",
    "corrupt",
    "damaged",
    "no page of this document could be rendered",  # core/extraction.py planner
    "fitz",
)

# Codes where the bytes themselves are the problem. Nothing about a retry can
# change the outcome, so the queue fails these on the first attempt instead of
# re-reading the same file and delaying the answer the user can act on.
PERMANENT_ERROR_CODES = frozenset({
    "FILE_CORRUPTED",
    "UNSUPPORTED_FORMAT",
    "FILE_MISSING",
})


def _is_unreadable_file(exc: Exception, msg: str) -> bool:
    for cls in type(exc).__mro__:
        if cls.__name__ in _CORRUPT_EXC_NAMES:
            return True
    return any(marker in msg for marker in _CORRUPT_MESSAGE_MARKERS)


def classify_extraction_error(exc: Exception, filename: str) -> tuple[int, str, str]:
    """Classify an extraction exception into (http_status, error_code, user_message)."""
    msg = str(exc).lower()

    if isinstance(exc, ValueError) and "not supported" in msg:
        return (400, "UNSUPPORTED_FORMAT",
                "This file format is not supported. Please upload a PDF, JPG, PNG, or XLSX.")

    # The local LLM endpoint not answering is its own case, checked before the
    # generic timeout/rate rungs below: a refused socket means nothing was sent
    # and nothing was extracted, so "try a simpler scan" and "the service is
    # busy" would both be wrong. Checked by exception type, so a hosted
    # provider's 429 never lands here.
    if is_local_endpoint_down(exc):
        return (503, LOCAL_LLM_UNREACHABLE_CODE, LOCAL_LLM_UNREACHABLE_MESSAGE)

    if "NOT_A_FINANCIAL_DOCUMENT" in str(exc):
        return (422, "NOT_FINANCIAL_DOCUMENT",
                "This file does not appear to be a financial document.")

    if _is_unreadable_file(exc, msg):
        if filename.lower().endswith(".pdf"):
            return (422, "FILE_CORRUPTED", "This file could not be read as a PDF.")
        return (422, "FILE_CORRUPTED",
                "This file could not be read — it appears to be corrupted or damaged. "
                "Please re-scan or re-download it.")

    if isinstance(exc, (asyncio.TimeoutError,)) or "timeout" in msg or "deadline" in msg:
        return (504, "TIMEOUT",
                "Extraction timed out. The file may be too complex. Try a simpler scan.")

    if any(k in msg for k in ("rate", "429", "quota")):
        return (503, "RATE_LIMITED",
                "Extraction service is temporarily busy. Please try again in a moment.")

    if any(k in msg for k in ("resolution", "quality", "blurry")):
        return (422, "IMAGE_QUALITY_LOW",
                "Image quality is too low for reliable extraction. Try a higher-resolution scan.")

    return (500, "EXTRACTION_ERROR",
            "Extraction failed unexpectedly. Please try again; the server log "
            "has the details.")
