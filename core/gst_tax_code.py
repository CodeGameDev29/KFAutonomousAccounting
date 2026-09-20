"""GST/HST tax-code assignment for accounting-package exports.

Why this module exists
----------------------
Deriving a tax code from the *category alone* — anything that is not revenue
or a transfer gets ``GST on Expenses`` — produces two filing-grade errors:

* Every ``Uncategorized`` row is tagged ``GST on Expenses``, so the engine
  claims an input tax credit on a row it admits it could not classify.
* A foreign supplier that charges no Canadian tax (an overseas host, a
  non-resident software vendor, fuel bought abroad) is tagged ``GST on
  Expenses``, and an inbound wire from a non-resident client is tagged
  ``GST on Income``.

Claiming an ITC on a supply that carried no GST overstates a refund claim.
That is the one class of error the engine must never produce, so this module
only ever asserts a GST code on **positive evidence** and otherwise asserts
nothing.

The rule
--------
GST/HST is recoverable (an ITC) only when a GST-registered supplier actually
charged it and the supporting document shows it. The engine cannot see a
supplier's registration status, so the only evidence it has is the tax amount
extracted from the linked document. Therefore:

===========================================  ===========================
Evidence                                     Tax code
===========================================  ===========================
Expense/COGS + linked doc shows GST or HST   ``GST on Expenses``
Revenue + linked doc shows GST or HST        ``GST on Income``
Supplier/payer is non-resident, no doc tax   ``Zero-rated / No GST``
Internal transfer / CRA remittance / draw    ``BAS Excluded``
Anything else (incl. ``Uncategorized``)      ``""`` — nothing asserted
===========================================  ===========================

An empty tax code is the honest answer, not a gap: Xero's bank-statement CSV
import has no tax field at all (you map Date / Amount / Payee / Description /
Reference), so the ``Tax Type`` column is read by a human coding the rows.
A blank there means "no evidence here; decide this one" — flagging for review
rather than guessing. The companion ``Tax Basis`` column says *why*, in words,
for every row.

Jurisdiction detection can only ever *remove* a GST claim (document tax is
checked first and always wins), so a false "foreign" reading downgrades a row
to ``Zero-rated / No GST`` and a false "Canadian" reading downgrades it to
blank. Neither can manufacture an input tax credit.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from config.ised_categories import UNCATEGORIZED_LABEL, kind_for, remap_legacy

# ─── Tax codes ────────────────────────────────────────────────────────────
# Xero's AU/NZ tax vocabulary, which these exports emit, plus an explicit
# no-GST value for a supply that carried none.
TAX_NONE = ""                                 # nothing asserted — review
TAX_GST_EXPENSES = "GST on Expenses"          # ITC claimable, doc shows tax
TAX_GST_INCOME = "GST on Income"              # GST/HST collected on a supply
TAX_NO_GST_FOREIGN = "Zero-rated / No GST"    # non-resident counterparty
TAX_BAS_EXCLUDED = "BAS Excluded"             # not a supply at all

# Matched by NAME, not by ``kind``. "Owner's draw" carries ``kind='expense'``
# because the project's taxonomy counts it as an operating cost in
# analytics and peer benchmarking — a reporting decision with nothing to say
# about GST. A distribution to the owner is not a supply, so it can never
# carry GST/HST regardless of what document turns up.
NOT_A_SUPPLY_NAMES: frozenset[str] = frozenset({"Owner's draw"})

# ─── Jurisdiction ─────────────────────────────────────────────────────────
CA = "CA"
FOREIGN = "FOREIGN"
UNKNOWN = "UNKNOWN"

CANADIAN_PROVINCE_CODES: frozenset[str] = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
})

# US state / territory codes a card-network memo can end with, glued to the
# city or separated by a space ("SAN FRANCISCOCA", "LOS ANGELES CA"). None of
# these collide with a Canadian province code, so the two sets are unambiguous.
US_STATE_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
})

# ISO-3166 alpha-3 codes that banks and card networks stamp on a memo. Only
# codes that would not plausibly appear as an English word or a common
# abbreviation are listed, so a whole-word match is safe.
FOREIGN_COUNTRY_CODES: frozenset[str] = frozenset({
    "USA", "DEU", "GBR", "IRL", "NLD", "FRA", "AUS", "NZL", "SGP", "JPN",
    "KOR", "CHN", "HKG", "TWN", "IND", "CHE", "SWE", "NOR", "DNK", "FIN",
    "ESP", "ITA", "PRT", "POL", "CZE", "ROU", "UKR", "BRA", "MEX", "ARG",
    "ZAF", "ARE", "ISR", "PHL", "VNM", "THA", "IDN", "MYS", "TUR", "EST",
    "LTU", "LVA", "BGR", "HRV", "GRC", "HUN", "SVK", "SVN", "AUT", "BEL",
    "LUX", "ISL", "COL", "CHL", "PER", "NGA", "KEN", "EGY", "SAU", "PAK",
    "BGD", "LKA", "NPL",
})

# Standalone two-letter region tokens a wire memo carries
# ("INCOMINGWIRE PAYMENT,US,BLUEPEAK"). Deliberately excludes "CA",
# which in a Canadian bank memo means Canada, not California — California is
# only ever read from the trailing city/state token.
FOREIGN_REGION_TOKENS: frozenset[str] = frozenset({"US", "UK", "EU", "GB"})

_TOKEN_RE = re.compile(r"[A-Za-z0-9$.]+")
# Shortest glued "CITYSTATE" token treated as a city + state code. Keeps
# 3-letter bank abbreviations ("ENT", "FAC", "DIV") out of the state test.
_MIN_GLUED_TOKEN_LEN = 5


def _tokens(text: str) -> list[str]:
    """Split a bank memo into word tokens, dropping separators and punctuation."""
    return [t.strip(".$") for t in _TOKEN_RE.findall(text or "") if t.strip(".$")]


def supplier_jurisdiction(description: str | None, vendor: str | None = None) -> str:
    """Classify the counterparty as Canadian, foreign, or unknown.

    Reads only what the memo and the extractor provide — an ISO-3 country
    code, a standalone region token, or the trailing city/state code of a
    card-network memo. Returns :data:`CA`, :data:`FOREIGN` or
    :data:`UNKNOWN`; never guesses from a vendor's brand name.
    """
    text = " ".join(p for p in (description, vendor) if p)
    tokens = _tokens(text)
    if not tokens:
        return UNKNOWN

    upper = [t.upper() for t in tokens]

    # 1. Explicit ISO-3 country code anywhere in the memo.
    for tok in upper:
        if tok in FOREIGN_COUNTRY_CODES:
            return FOREIGN
        if tok == "CAN":
            return CA

    # 2. Standalone region token ("...,US,...").
    for tok in upper:
        if tok in FOREIGN_REGION_TOKENS:
            return FOREIGN

    # 3. Trailing city/state token: "LOS ANGELES CA" or "SAN FRANCISCOCA".
    last = upper[-1]
    if len(last) == 2:
        if last in CANADIAN_PROVINCE_CODES:
            return CA
        if last in US_STATE_CODES:
            return FOREIGN
    elif len(last) >= _MIN_GLUED_TOKEN_LEN and last.isalpha():
        if last[-2:] in CANADIAN_PROVINCE_CODES:
            return CA
        if last[-2:] in US_STATE_CODES:
            return FOREIGN

    return UNKNOWN


# ─── Document tax evidence ────────────────────────────────────────────────

def _as_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def document_shows_gst(doc_tax: dict | None) -> bool:
    """True iff a linked document records a non-zero GST **or** HST amount.

    ``doc_tax`` is ``{"has_document": bool, "gst": …, "hst": …}``. A provincial
    sales tax and "other" tax are deliberately ignored: neither is recoverable
    as a GST/HST input tax credit. That holds whichever name the province gives
    it — PST in British Columbia and Saskatchewan, RST in Manitoba, QST in
    Quebec — so no provincial label is read here at all; a provincial amount
    reaches the extractor's ``pst`` field and this function never looks at it.
    """
    if not doc_tax:
        return False
    gst = _as_decimal(doc_tax.get("gst"))
    hst = _as_decimal(doc_tax.get("hst"))
    return (gst + hst) > 0


def has_linked_document(doc_tax: dict | None) -> bool:
    """True iff a source document is linked to the row at all."""
    return bool(doc_tax and doc_tax.get("has_document"))


# ─── The assignment ───────────────────────────────────────────────────────

def xero_tax_code(
    category: str | None,
    *,
    doc_tax: dict | None = None,
    description: str | None = None,
    vendor: str | None = None,
) -> tuple[str, str]:
    """Return ``(tax_code, basis)`` for one exported row.

    ``basis`` is a short human sentence naming the evidence the code rests on
    — it is written to the export's ``Tax Basis`` column so an accountant can
    see at a glance which rows the engine decided and which it handed back.

    A GST code is returned **only** when ``doc_tax`` shows GST or HST on the
    linked document. Every other outcome either asserts no tax
    (``Zero-rated / No GST``, ``BAS Excluded``) or asserts nothing (``""``).
    """
    canonical = remap_legacy(category)
    jurisdiction = supplier_jurisdiction(description, vendor)

    if not canonical or canonical == UNCATEGORIZED_LABEL:
        # No tax code at all: the engine could not classify the row, so it has
        # no business asserting its tax treatment either — flag for review
        # rather than guess. The jurisdiction still goes in the basis,
        # because "this supplier is not resident in Canada" is a fact the
        # reviewer wants even when the category is unknown.
        note = (
            " (non-resident counterparty — no Canadian GST/HST expected)"
            if jurisdiction == FOREIGN else ""
        )
        return TAX_NONE, f"Uncategorized — flagged for review, no tax code asserted{note}"

    kind = kind_for(canonical)
    if kind == "none":
        return TAX_NONE, "Category outside the CRA/ISED taxonomy — no tax code asserted"

    if canonical in NOT_A_SUPPLY_NAMES:
        return TAX_BAS_EXCLUDED, "Owner distribution — not a supply, no GST/HST"
    if kind == "transfer":
        return TAX_BAS_EXCLUDED, "Internal movement between own accounts — not a supply"
    if kind == "tax":
        return TAX_BAS_EXCLUDED, "Remittance to CRA — not a supply"
    if kind == "equity":
        return TAX_BAS_EXCLUDED, "Owner distribution — not a supply"
    if kind == "contra":
        return TAX_NONE, "Refund/credit — GST follows the original supply, flagged for review"

    tax_on_doc = document_shows_gst(doc_tax)
    linked = has_linked_document(doc_tax)

    if kind == "income":
        if tax_on_doc:
            return TAX_GST_INCOME, "GST/HST shown as collected on the linked sales document"
        if jurisdiction == FOREIGN:
            return (
                TAX_NO_GST_FOREIGN,
                "Non-resident payer — no Canadian GST/HST collected on this supply",
            )
        if linked:
            return TAX_NONE, "Linked sales document shows no GST/HST — flagged for review"
        return TAX_NONE, "No sales document linked — no tax code asserted"

    # cogs / cogs_contra / expense
    if tax_on_doc:
        return TAX_GST_EXPENSES, "GST/HST shown on the linked document — ITC claimable"
    if jurisdiction == FOREIGN:
        return (
            TAX_NO_GST_FOREIGN,
            "Non-resident supplier — no Canadian GST/HST charged, nothing to claim",
        )
    if linked:
        return TAX_NONE, "Linked document shows no GST/HST — no ITC claimed"
    return TAX_NONE, "No document linked — no tax code asserted, flagged for review"
