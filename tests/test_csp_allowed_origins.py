"""The Content-Security-Policy is an allow-list, and this file is its inventory.

Every source the policy names is checked against a closed set: the same origin,
the two inline-data schemes the app renders from, one hashed inline script, the
inline styles the component library emits, and the two Google hosts that
optional Sign in with Google needs. Anything else — any scheme, any host, any
wildcard — fails here, so widening the policy is a deliberate edit to this list
rather than a one-line change nobody reads.

The mirror-image half matters just as much: the Google hosts must stay present,
because dropping one of those shows up as a broken sign-in in the browser
instead of a failing test.

Implementation note: the CSP string is hard-coded in the ``add_security_headers``
middleware in ``server/app.py``. It is read straight from the module source and
parsed here, rather than importing and reloading the app, which would clobber
other tests' MagicMock state.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_APP_PY = Path(__file__).resolve().parent.parent / "server" / "app.py"

# ── The allow-list ───────────────────────────────────────────────────────────

# Keywords carry no origin: they name the document's own origin, nothing at all,
# or a hash of one inline script that is checked in with the policy.
_ALLOWED_KEYWORDS = {"'self'", "'none'"}
_HASH_RE = re.compile(r"^'sha(?:256|384|512)-[A-Za-z0-9+/]+={0,2}'$")

# Schemes that serve bytes the app itself produced: rasterised page images and
# object URLs for a PDF preview.
_ALLOWED_SCHEMES = {"data:", "blob:"}

# The only remote origins in the policy. Both belong to optional Sign in with
# Google: the OAuth endpoint the button hands the browser to, and the image host
# a Google-created account's avatar is served from.
_ALLOWED_HOSTS = {
    "https://accounts.google.com",
    "https://*.googleusercontent.com",
}

# ``'unsafe-inline'`` is a real widening, so it is allowed in exactly one
# directive: the component library sets element styles inline, and a hash cannot
# cover styles computed at render time. Scripts are covered by a hash instead.
_UNSAFE_INLINE_DIRECTIVES = {"style-src"}


def _extract_csp() -> str:
    """Extract the CSP literal the middleware sets, from server/app.py source."""
    src = _APP_PY.read_text(encoding="utf-8")
    m = re.search(
        r'response\.headers\["Content-Security-Policy"\]\s*=\s*\((.*?)\)',
        src,
        re.DOTALL,
    )
    if not m:
        raise AssertionError("could not locate CSP literal in server/app.py")
    block = m.group(1)
    chunks = re.findall(r'"([^"]*)"', block)
    return "".join(chunks)


def _csp_directives(csp: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for chunk in csp.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split()
        out[parts[0]] = parts[1:]
    return out


def _classify(source: str, directive: str) -> str | None:
    """Return None when the source is allowed, or why it is not."""
    if source in _ALLOWED_KEYWORDS or _HASH_RE.match(source):
        return None
    if source == "'unsafe-inline'":
        if directive in _UNSAFE_INLINE_DIRECTIVES:
            return None
        return "inline sources are allowed only in " + ", ".join(sorted(_UNSAFE_INLINE_DIRECTIVES))
    if source in _ALLOWED_SCHEMES:
        return None
    if source in _ALLOWED_HOSTS:
        return None
    return "not in the allow-list at the top of this file"


def test_csp_allows_only_known_origins():
    """Every source in every directive is one the allow-list names.

    Checked across the whole policy, not just connect-src: an origin is equally
    reachable whether it arrives through script-src, img-src or frame-src.
    """
    directives = _csp_directives(_extract_csp())
    assert directives, "the policy parsed to no directives at all"

    offenders = [
        (directive, source, why)
        for directive, sources in directives.items()
        for source in sources
        if (why := _classify(source, directive)) is not None
    ]
    assert not offenders, (
        "Content-Security-Policy names sources outside the allow-list: "
        + "; ".join(f"{d} {s!r} — {why}" for d, s, why in offenders)
        + ". Widening the policy means adding the origin to the allow-list in "
        "this file, deliberately, with a reason."
    )


def test_csp_default_src_is_self():
    """Everything not explicitly allowed falls back to same-origin."""
    directives = _csp_directives(_extract_csp())
    assert directives.get("default-src") == ["'self'"]


def test_csp_frame_ancestors_is_none():
    """No other page may frame the app."""
    directives = _csp_directives(_extract_csp())
    assert directives.get("frame-ancestors") == ["'none'"]


@pytest.mark.parametrize(
    "directive,host",
    [
        ("script-src", "https://accounts.google.com"),
        ("connect-src", "https://accounts.google.com"),
        ("img-src", "https://*.googleusercontent.com"),
    ],
)
def test_csp_keeps_google_sign_in_reachable(directive, host):
    """The origins Sign in with Google needs must stay in the policy.

    Dropping one breaks the sign-in button and the avatar it loads afterwards,
    which is a browser-only failure unless it is asserted here.
    """
    directives = _csp_directives(_extract_csp())
    assert host in directives.get(directive, []), (
        f"{directive} no longer names {host!r}. Currently: {directives.get(directive)}"
    )
