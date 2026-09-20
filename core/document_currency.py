"""What currency is a proof of transaction in — and on what evidence?

A receipt that prints a bare ``$`` beside a US mailing address is not evidence
of USD. An extraction prompt that says *"For $ in Canadian context, use CAD.
For US$, use USD"* makes the model read the address, guess a currency, and have
that guess stored in ``documents.currency`` with exactly the same weight as a
receipt that prints ``USD`` in full. The pair then fails on amount: a Canadian
card line against its own "USD" receipt scores **Amount 0** and is explained as
cross-currency amounts that do not reconcile at any rate inside the engine's
CAD/USD band.

A tax line settles it without guessing: tax at 12.0% of subtotal is 5% GST plus
a 7% provincial sales tax, which no US jurisdiction charges, so the charge is
Canadian.

This module is the one place that answers "what currency, and on what
evidence", for extraction, for matching, for the explanation a user reads:

* ``stated``   — the document itself says so (an ISO code, ``US$``, ``C$``, €, £).
* ``inferred`` — read off a Canadian tax line (below).
* ``assumed``  — nothing said, nothing inferable. The code is ``None``, and a
  comparison reads the receipt in the transaction's currency rather than
  scoring it zero for disagreeing with a currency nobody ever wrote down.

The distinction is the whole point: a *stated* USD invoice against a CAD bank
row must still fail, while an *unstated* one must not.

## Why the model is asked to quote, not to decide

An instruction to "emit ``currency`` only when the document states one" still
leaves the model deciding whether a bare ``$`` beside a US address counts as
stating USD, and on a store receipt it decides that it does. So extraction asks
for ``currency_marker`` as well — **the currency text printed next to the total,
copied across character for character** — and that quotation, not the model's
reading, is what earns ``stated``:

* The marker names exactly one currency (``US$``, ``CAD``, ``€``, ``¥``) →
  that currency, ``stated``. The marker outranks the model's ``currency`` when
  the two disagree, because one of them is a transcription and the other is an
  opinion.
* The marker is a bare ``$`` — or nothing was printed, or the key never came
  back — → the model's ``currency`` is **not** trusted as stated. A Canadian tax
  line makes it ``("CAD", "inferred")``; otherwise the answer is
  ``(None, "assumed")``.

That last choice is deliberate. Keeping the model's code as *inferred* instead
would look more informative and be worse: an "inferred USD" read off a US
address still disagrees with a CAD bank line, and
:func:`core.amount_match.compare_amounts` would still score the amount zero.
``assumed`` is the honest state — nobody wrote a currency down —
and it is the state that lets the comparison read the receipt in the
transaction's own currency and say so.

The residual case, an unreadable marker that carries no dollar sign at all
(``"kr"``, ``"zł"``), keeps the model's code as ``stated``: whatever misled it,
it was not the ambiguity between one country's dollar and another's. An
unreadable marker that *does* carry a dollar sign stays ambiguous, because an
unrecognised qualifier is as likely to be a stray word the model quoted along
with the sign ("Total $") as it is to be an unlisted currency.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

# ── Provenance ────────────────────────────────────────────────────────────
STATED = "stated"
INFERRED = "inferred"
ASSUMED = "assumed"
VALID_SOURCES = (STATED, INFERRED, ASSUMED)

# A currency code is an ISO-4217-shaped token. Anything else a model might
# emit for a bare dollar sign — "$", "CA$", "dollars", "" — is not a code and
# carries no information about which dollar it is.
_CODE_RE = re.compile(r"^[A-Z]{3}$")

# ── The currency marker ───────────────────────────────────────────────────
# The extraction payload key holding the currency text the document literally
# prints beside its total, quoted verbatim by the model.
MARKER_FIELD = "currency_marker"

# What a marker turned out to be. Returned by :func:`classify_currency_marker`.
MARKER_NAMED = "named"            # names exactly one currency: US$, CAD, €, ¥
MARKER_DOLLAR = "dollar"          # a dollar sign that names no country
MARKER_ABSENT = "absent"          # nothing printed, or the key never came back
MARKER_UNREADABLE = "unreadable"  # something was printed; it is not recognised

# ISO codes worth recognising in a marker. A whitelist rather than "any three
# capitals", so "TAX", "CAN" or "NET" printed beside a total is not read as a
# currency.
ISO_CODES = frozenset({
    "AED", "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP",
    "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN", "MYR", "NOK",
    "NZD", "PHP", "PLN", "RUB", "SEK", "SGD", "THB", "TRY", "TWD", "USD",
    "VND", "ZAR",
})
# Three-letter tokens that name a currency without being its ISO code.
_CODE_ALIASES = {"RMB": "CNY", "CNH": "CNY", "NTD": "TWD"}

# Symbols that name exactly one currency. ¥ is ambiguous between CNY and JPY and
# is read as CNY, to agree with the other tokens listed beside it here — 人民币,
# 元, CNH, RMB — which are all Chinese. Change it to JPY if the suppliers whose
# paperwork this reads are Japanese; the symbol alone cannot be right for both.
_SYMBOL_CODES = (
    ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("₩", "KRW"), ("₽", "RUB"),
    ("₪", "ILS"), ("₺", "TRY"), ("฿", "THB"), ("₫", "VND"), ("₱", "PHP"),
    ("人民币", "CNY"), ("¥", "CNY"), ("￥", "CNY"), ("元", "CNY"),
)

# A dollar sign says nothing on its own; the letters beside it say everything.
# Keys are compared with periods stripped, so "U.S.$" and "US$" are one entry.
_DOLLAR_QUALIFIERS = {
    "US": "USD", "USA": "USD", "AMERICAN": "USD",
    "C": "CAD", "CA": "CAD", "CDN": "CAD", "CAN": "CAD", "CANADIAN": "CAD",
    "A": "AUD", "AU": "AUD", "AUS": "AUD", "AUSTRALIAN": "AUD",
    "NZ": "NZD", "NZL": "NZD", "HK": "HKD", "SG": "SGD", "S": "SGD",
    "NT": "TWD", "MX": "MXN", "MEX": "MXN",
}
_DOLLAR_SIGNS = "$＄"
_ISO_TOKEN_RE = re.compile(r"\b[A-Z]{3}\b")
_DOLLARISH_RE = re.compile(rf"[{_DOLLAR_SIGNS}]|DOLLAR")
_QUALIFIED_DOLLAR_RE = re.compile(
    rf"([A-Z.]{{1,10}})\s*[{_DOLLAR_SIGNS}]|[{_DOLLAR_SIGNS}]\s*([A-Z.]{{1,10}})"
)
_DOLLAR_WORD_RE = re.compile(r"\b([A-Z.]{1,10})\s+DOLLARS?\b")

# Tokens that mean "the key came back empty" rather than "the document printed
# this". A model asked for a verbatim quotation answers one of these when there
# is nothing to quote.
_EMPTY_MARKERS = frozenset({"", "NULL", "NONE", "N/A", "NA", "-", "—", "NIL"})

# ── The Canadian tax signal ───────────────────────────────────────────────
# Combined sales-tax rates charged in Canada, in percent: 5 (GST alone, AB/NT/
# NU/YT and every GST-only supply), 12 (5% GST + a 7% provincial sales tax),
# 13 (13% HST), 14 (a pre-2016 HST rate still seen on old receipts), 15 (15%
# HST). Nothing charged
# in the United States lands on these to within a third of a point — state and
# local sales tax combines to 4–10.25% in fractions like 8.875%.
CANADIAN_TAX_RATES_PCT: tuple[Decimal, ...] = (
    Decimal("5"), Decimal("12"), Decimal("13"), Decimal("14"), Decimal("15"),
)
# Slack for a receipt that rounds its tax line to the cent.
TAX_RATE_TOLERANCE_PCT = Decimal("0.3")

# The document columns this module reads, for a SELECT that wants to resolve a
# currency from a raw row. ``currency`` itself is normally already selected.
CURRENCY_RESOLUTION_COLUMNS = (
    "currency_source, subtotal, tax_gst, tax_hst, tax_pst, tax_other"
)


def _get(doc: Any, *names: str) -> Any:
    """First non-None value among ``names`` on a mapping or an object.

    Extraction payloads spell the tax fields ``gst``/``hst``/``pst``/
    ``other_tax``; ``Document`` and the ``documents`` table spell them
    ``tax_gst``/``tax_hst``/``tax_pst``/``tax_other``. Callers should not have
    to care which shape they hold.
    """
    for name in names:
        try:
            if isinstance(doc, Mapping):
                value = doc.get(name)
            else:
                value = getattr(doc, name, None)
        except Exception:
            value = None
        if value is not None:
            return value
    return None


def _num(value: Any) -> Decimal | None:
    """Parse a money-ish value to Decimal, or None when it is not a number.

    Never raises: a document row can hold anything a model once emitted.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except Exception:
            return None
    try:
        text = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    except Exception:
        return None
    text = re.sub(r"[A-Za-z]+$", "", text).strip()
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def normalize_currency_code(value: Any) -> str | None:
    """``"usd"`` → ``"USD"``; ``"$"``, ``""``, ``None``, junk → ``None``."""
    try:
        text = str(value or "").strip().upper()
    except Exception:
        return None
    return text if _CODE_RE.match(text) else None


def normalize_currency_source(value: Any) -> str | None:
    """One of ``stated``/``inferred``/``assumed``, or None for anything else."""
    try:
        text = str(value or "").strip().lower()
    except Exception:
        return None
    return text if text in VALID_SOURCES else None


def _marker_text(marker: Any) -> str:
    """A marker normalised for matching: upper-cased, trimmed, "" when empty.

    ``None``, a missing key and the placeholders a model reaches for when there
    is nothing to quote ("none", "n/a", "-") all collapse to ``""`` — they are
    the same statement: the document printed no currency text.
    """
    try:
        text = str(marker if marker is not None else "").strip().upper()
    except Exception:
        return ""
    return "" if text in _EMPTY_MARKERS else text


def currency_from_marker(marker: Any) -> str | None:
    """The one currency a printed marker can only mean, or None.

    ``"US$"`` → USD, ``"C$"`` → CAD, ``"CAD"`` → CAD, ``"€"`` → EUR,
    ``"¥"``/``"RMB"`` → CNY. A bare ``"$"``, an empty marker and anything
    unrecognised → ``None``. Never raises.
    """
    text = _marker_text(marker)
    if not text:
        return None

    for token in _ISO_TOKEN_RE.findall(text):
        if token in ISO_CODES:
            return token
        alias = _CODE_ALIASES.get(token)
        if alias:
            return alias

    for symbol, code in _SYMBOL_CODES:
        if symbol in text:
            return code

    for pattern in (_QUALIFIED_DOLLAR_RE, _DOLLAR_WORD_RE):
        match = pattern.search(text)
        if not match:
            continue
        qualifier = next((g for g in match.groups() if g), "").replace(".", "").strip()
        code = _DOLLAR_QUALIFIERS.get(qualifier)
        if code:
            return code
    return None


def classify_currency_marker(marker: Any) -> tuple[str | None, str]:
    """``(code, kind)`` for a marker the model quoted off the document.

    ``kind`` is :data:`MARKER_NAMED` (the code is the answer),
    :data:`MARKER_DOLLAR` (a dollar sign that names no country),
    :data:`MARKER_ABSENT` (nothing printed / key not returned) or
    :data:`MARKER_UNREADABLE` (something was printed, it is not recognised, and
    it is not a dollar sign).
    """
    text = _marker_text(marker)
    if not text:
        return None, MARKER_ABSENT
    code = currency_from_marker(text)
    if code:
        return code, MARKER_NAMED
    if _DOLLARISH_RE.search(text):
        return None, MARKER_DOLLAR
    return None, MARKER_UNREADABLE


# A currency marker is only evidence when it is printed *beside a figure*: the
# "USD" in a footer about wire fees is not what this invoice is billed in.
_TEXT_AMOUNT = r"\d[\d,]*\.\d{2}"
_TEXT_MARKERS = (
    r"US\$|U\.S\.\$|CA\$|C\$|CAD\$|A\$|NZ\$|HK\$|S\$|"
    r"USD|CAD|EUR|GBP|CNY|RMB|JPY|AUD|NZD|HKD|SGD|CHF|MXN|INR"
)
_TEXT_MARKER_RE = re.compile(
    rf"(?<![A-Za-z])(?P<pre>{_TEXT_MARKERS})\s{{0,2}}\$?\s{{0,2}}{_TEXT_AMOUNT}"
    rf"|{_TEXT_AMOUNT}\s{{0,2}}(?P<post>{_TEXT_MARKERS})(?![A-Za-z])",
    re.IGNORECASE,
)


def marker_from_text(text: Any) -> str | None:
    """The currency marker a PDF's own text layer prints beside its amounts.

    Machine-generated invoices come through extraction as text, not pixels
    (``core/extraction.py``'s ``mode="text"`` plan), and text is a better
    witness than a model's transcription of it. Returns the marker only when
    **every** clear marker in the text agrees — an invoice that names two
    currencies has not stated which one it is billed in — and never returns a
    bare ``"$"``, which is not evidence of anything. Never raises.
    """
    try:
        body = str(text or "")
    except Exception:
        return None
    if not body:
        return None

    found: str | None = None
    code: str | None = None
    try:
        for match in _TEXT_MARKER_RE.finditer(body):
            marker = (match.group("pre") or match.group("post") or "").strip()
            this = currency_from_marker(marker)
            if this is None:
                continue
            if code is None:
                code, found = this, marker
            elif this != code:
                return None  # the text names more than one currency
    except Exception:
        return None
    return found


def canadian_tax_signal(doc: Any) -> bool:
    """True when the document's own tax figures can only be Canadian.

    Two ways to fire, and neither one guesses:

    1. A **named** Canadian tax carries a non-zero figure — ``tax_gst``,
       ``tax_hst`` or ``tax_pst``. GST/HST/PST are Canadian statutes; a US
       receipt does not have a PST line.
    2. The **rate** is a Canadian rate. Total tax over subtotal lands within
       ``TAX_RATE_TOLERANCE_PCT`` of 5 / 12 / 13 / 14 / 15 percent. Total tax is
       the sum of the tax fields the document carries, or ``total - subtotal``
       when it carries none at all.

    Never raises; a document row that holds a ``set()`` where a number belongs
    simply has no signal.
    """
    try:
        gst = _num(_get(doc, "tax_gst", "gst"))
        hst = _num(_get(doc, "tax_hst", "hst"))
        pst = _num(_get(doc, "tax_pst", "pst"))
        if any(v is not None and v != 0 for v in (gst, hst, pst)):
            return True

        other = _num(_get(doc, "tax_other", "other_tax"))
        subtotal = _num(_get(doc, "subtotal"))
        if subtotal is None or subtotal <= 0:
            return False

        named = [v for v in (gst, hst, pst, other) if v is not None]
        if named:
            tax = sum(named, Decimal(0))
        else:
            total = _num(_get(doc, "total"))
            if total is None:
                return False
            tax = total - subtotal
        if tax <= 0:
            return False

        rate_pct = tax / subtotal * Decimal(100)
        return any(
            abs(rate_pct - rate) <= TAX_RATE_TOLERANCE_PCT
            for rate in CANADIAN_TAX_RATES_PCT
        )
    except Exception:
        return False


def resolve_document_currency(doc: Any) -> tuple[str | None, str]:
    """``(code, source)`` for a stored document. The one rule, in order:

    1. The document **stated** a code — return it. A stated USD invoice stays
       USD however Canadian its tax line looks, so a cross-currency pair is
       never quietly turned into a same-currency one.
    2. A **Canadian tax signal** — ``("CAD", "inferred")``. Applied to rows
       whose source is ``inferred``, ``assumed`` *or* NULL, so a row that says
       USD beside a 12% tax line is read as the CAD charge it is. A row that
       already said CAD keeps its plain ``stated`` reading: the inference agrees
       with it, so there is nothing to override.
    3. A row carrying **no ``currency_source`` value at all** but a well-formed
       code — trusted as stated, since a code with no recorded provenance is
       still a code somebody wrote down.
    4. An ``inferred`` code — kept, with its provenance.
    5. Anything else — ``(None, "assumed")``. No code, a bare ``"$"``, a junk
       token, or a row that admits it assumed: the currency is unknown, and
       :func:`core.amount_match.compare_amounts` reads the receipt in the bank
       line's currency rather than failing it for disagreeing with a guess.
    """
    code = normalize_currency_code(_get(doc, "currency"))
    source = normalize_currency_source(_get(doc, "currency_source"))

    if source == STATED and code:
        return code, STATED
    if canadian_tax_signal(doc):
        if code == "CAD" and source is None:
            # A row that already said CAD, beside a tax line that says the
            # same. There is nothing to override and nothing to explain, so it
            # stays a plain stated reading, and a Canadian receipt with a stated
            # code scores, prompts and reads the same whether or not the row
            # recorded a provenance.
            return "CAD", STATED
        return "CAD", INFERRED
    if code and source is None:
        return code, STATED
    if code and source == INFERRED:
        return code, INFERRED
    return None, ASSUMED


def currency_from_extraction(extraction_data: Any) -> tuple[str | None, str]:
    """``(code, source)`` for a freshly extracted payload. In order:

    1. ``currency_marker`` **names** a currency (``US$``, ``CAD``, ``€``, ``¥``)
       — that code, ``stated``. The quotation outranks the model's ``currency``
       when they disagree; see the module docstring for why.
    2. The marker is a bare ``$``, or nothing was printed, or the key never came
       back — the model's ``currency`` is not evidence. A Canadian tax line then
       gives ``("CAD", "inferred")``, and nothing at all gives
       ``(None, "assumed")``.
    3. The marker is unreadable but is not a dollar sign — the model's code is
       kept as ``stated``, because dollar ambiguity is not what could have
       misled it.
    """
    code, kind = classify_currency_marker(
        _get(extraction_data, MARKER_FIELD, "currency_symbol")
    )
    if code:
        return code, STATED

    model_code = normalize_currency_code(_get(extraction_data, "currency"))
    if kind == MARKER_UNREADABLE and model_code:
        return model_code, STATED
    if canadian_tax_signal(extraction_data):
        return "CAD", INFERRED
    return None, ASSUMED


def currency_fields(currency: Any, resolution_columns: Any) -> dict:
    """Build what :func:`resolve_document_currency` reads from a raw DB row.

    ``resolution_columns`` is the tail of a SELECT that ended with
    :data:`CURRENCY_RESOLUTION_COLUMNS`, in that order.
    """
    values = list(resolution_columns or [])
    values += [None] * (6 - len(values))
    source, subtotal, gst, hst, pst, other = values[:6]
    return {
        "currency": currency,
        "currency_source": source,
        "subtotal": subtotal,
        "tax_gst": gst,
        "tax_hst": hst,
        "tax_pst": pst,
        "tax_other": other,
    }
