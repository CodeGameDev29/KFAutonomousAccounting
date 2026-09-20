# config/ised_categories.py
"""Canonical, fixed Canada ISED/GIFI categorization taxonomy — the ONE source of truth.

The categorization agent CLASSIFIES into this closed list; it never invents,
renames, merges, or deletes a category name. Peer benchmarking
(``core/reasonableness.py``) shares this vocabulary via each row's ``ised_bucket``,
which is identical to the reasonableness GIFI bucket slugs.

The contract the rest of the engine relies on:
  * ``coerce_to_valid`` returns ``None`` for off-list / journal-only names; the
    classifier/persistence layer writes SQL ``NULL`` and the UI displays
    "Uncategorized" via ``COALESCE(category, 'Uncategorized')``. The literal
    string "Uncategorized" is NEVER written to ``transactions.category``.
  * ``kind`` is the rich accounting value (income|cogs|cogs_contra|expense|
    transfer|equity|tax|contra) — read by reasonableness and the Xero export. It
    is persisted as-is to ``user_categories.kind`` (no CHECK constraint).
    ONLY ``kind='transfer'`` removes a row from raw money totals
    (``core/transfer_exclusion.py``), so wages / owner's draw / tax are
    deliberately NOT 'transfer'.
  * ``Owner's draw`` and ``Income tax and GST/HST remittance`` are
    ``excluded_from_pl=False`` — shown as distinct P&L lines. Only the two pure
    transfers (``Credit card payment`` / ``Internal transfer``) are
    ``excluded_from_pl=True``.

This module imports NOTHING from the app (``config/settings.py`` imports it, so
it must have no import cycle).

Adding a row is a taxonomy change, not a configuration change: every stored
category name, migration and export is keyed on this list.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Category:
    name: str                       # exact canonical string (DB value + picker label)
    kind: str                       # income|cogs|cogs_contra|expense|transfer|equity|tax|contra
    section: str                    # revenue|cost_of_sales|operating|structural  (picker grouping)
    ised_bucket: str | None         # rollup slug shared with reasonableness / ised_benchmarks.json; None = no benchmark line
    gifi: str | None                # CRA GIFI line code(s) for the audit binder / T2; None for structural
    journal_only: bool = False      # period journal entry; never assignable to a bank txn
    excluded_from_pl: bool = False  # True ONLY for the two pure transfers: not a P&L line, avoids double-count
    is_operating_revenue: bool = False   # contributes to the benchmark operating-revenue denominator
    cra_scrutiny_weight: float = 1.0     # >1.0 up-weights a CRA-scrutinized line (reasonableness projection)
    is_generic: bool = False             # non-descriptive catch-all bucket (reasonableness projection)
    definition: str = ""            # one-line definition injected into every LLM prompt
    example_keywords: tuple[str, ...] = field(default_factory=tuple)


# Order matters: it is the picker/section display order and the seed sort_order.
ISED_CATEGORIES: tuple[Category, ...] = (
    # ── Revenue (kind = income) ──
    Category(
        "Sales of goods and services", "income", "revenue", "operating_revenue", "8000/8299",
        is_operating_revenue=True,
        definition="Earned operating revenue from clients/customers (the only line in the operating-revenue denominator).",
        example_keywords=("INCOMING WIRE", "DirectDeposit", "processor payout", "invoice paid",
                          "e-Transfer received", "Client Revenue"),
    ),
    Category(
        "All other revenues", "income", "revenue", "non_operating", "8230",
        definition="Non-operating income: interest earned, grants, tax refunds, capital injections (excluded from the operating denominator).",
        example_keywords=("INTEREST EARNED", "GST refund", "grant", "capital injection", "loan proceeds"),
    ),

    # ── Cost of sales / direct (kind = cogs) ──
    Category(
        "Wages and benefits", "cogs", "cost_of_sales", "cost_of_sales", "8340",
        definition="DIRECT/production labour that is a cost of the goods or services actually sold (cost of sales; explicit production signal only).",
        example_keywords=("production wages", "direct labour", "shop wages", "job cost"),
    ),
    Category(
        "Purchases, materials and sub-contracts", "cogs", "cost_of_sales", "subcontracts", "8320",
        cra_scrutiny_weight=1.5,
        definition="Direct materials, goods for resale, and contractor/subcontractor payments delivering client work.",
        example_keywords=("WISE PAYMENTS", "WISEMSP", "PAYPALMSP", "OUTGOING WIRE", "UPWORK",
                          "ODESK", "raw materials", "Contractor Fees"),
    ),
    Category(
        "Opening inventory", "cogs", "cost_of_sales", "cost_of_sales", "8300",
        journal_only=True,
        definition="Value of inventory on hand at the START of the period. JOURNAL ENTRY ONLY — never a bank transaction.",
        example_keywords=("opening stock",),
    ),
    Category(
        "Closing inventory", "cogs_contra", "cost_of_sales", "cost_of_sales", "8500",
        journal_only=True,
        definition="Value of inventory on hand at the END of the period (contra reducing COGS). JOURNAL ENTRY ONLY.",
        example_keywords=("closing stock",),
    ),

    # ── Operating / indirect (kind = expense) ──
    Category(
        "Labour and commissions", "expense", "operating", "salaries_wages", "9060",
        definition="INDIRECT admin/sales labour, payroll, and commissions — the DEFAULT bucket for service-firm labour.",
        example_keywords=("Cheque NO.", "[CK]NO.", "payroll", "salary", "commission"),
    ),
    Category(
        "Amortization and depletion", "expense", "operating", "cca_depreciation", "8670",
        journal_only=True,
        definition="Non-cash amortization / depreciation / CCA. JOURNAL ENTRY ONLY — never a bank transaction.",
        example_keywords=("depreciation", "CCA", "amortization"),
    ),
    Category(
        "Repairs and maintenance", "expense", "operating", "repairs_maintenance", "8960",
        definition="Repairs, cleaning, and upkeep of premises and equipment.",
        example_keywords=("repair", "maintenance", "servicing", "HVAC repair"),
    ),
    Category(
        "Utilities and telephone/telecommunication", "expense", "operating", "utilities", "9220/9225",
        definition="Hydro, gas, water, phone, internet, telecom service charges.",
        example_keywords=("telecom", "wireless", "mobile plan", "internet service", "hydro"),
    ),
    Category(
        "Rent", "expense", "operating", "rent", "8910",
        definition="Office/premises rent and leases.",
        example_keywords=("rent", "lease", "WeWork", "coworking"),
    ),
    Category(
        "Interest and bank charges", "expense", "operating", "interest_bank", "8710/8715",
        definition="Bank fees, service charges, loan interest PAID, NSF.",
        example_keywords=("MONTHLY FEE", "TRANSACTION FEE", "[SC]", "INTERAC e-Transfer Fee",
                          "ReturnedItemFee", "NSF"),
    ),
    Category(
        "Professional and business fees", "expense", "operating", "professional_fees", "8860",
        cra_scrutiny_weight=1.5,
        definition="LEGAL, ACCOUNTING, NOTARY, CONSULTING for the business itself (NOT client-delivery contractors — those are Purchases, materials and sub-contracts).",
        example_keywords=("legal", "accounting", "notary", "consulting", "bookkeeper"),
    ),
    Category(
        "Advertising and promotion", "expense", "operating", "advertising", "8520",
        definition="Marketing, ads, website, and promotional spend.",
        example_keywords=("MAILCHIMP", "META ADS", "Google Ads", "SQUARESPACE", "WIXSITE"),
    ),
    Category(
        "Delivery, shipping and warehouse expenses", "expense", "operating", "delivery", "9275",
        definition="Freight, courier, postage, warehousing.",
        example_keywords=("courier", "postage", "shipping", "freight", "parcel delivery",
                          "MERIDIAN COURIER"),
    ),
    Category(
        "Insurance", "expense", "operating", "insurance", "8690",
        definition="Business insurance premiums.",
        example_keywords=("insurance premium", "liability insurance", "policy renewal"),
    ),
    Category(
        "Travel", "expense", "operating", "travel", "9200",
        cra_scrutiny_weight=2.5,
        definition="Airfare, hotel, lodging, and public/ground transit for business travel (fuel/parking -> Motor vehicle expenses).",
        example_keywords=("airfare", "flight", "hotel", "rideshare", "rail", "taxi"),
    ),
    Category(
        "Meals and entertainment", "expense", "operating", "meals_entertainment", "8523",
        cra_scrutiny_weight=2.5,
        definition="Business meals and entertainment (CRA 50% rule).",
        example_keywords=("restaurant", "meal", "food delivery", "coffee shop", "client dinner"),
    ),
    Category(
        "Office supplies", "expense", "operating", "office_supplies", "8811",
        definition="Consumable office supplies, small equipment, general marketplace buys.",
        example_keywords=("office supply store", "stationery", "STAPLES", "IKEA", "Amazon"),
    ),
    Category(
        "Software and IT", "expense", "operating", "it_software", "8760",
        definition="SaaS, cloud, software subscriptions, hosting, developer tools, IT services.",
        example_keywords=("OPENAI", "AWS", "GOOGLE GSUITE", "JETBRAINS", "ADOBE", "ANTHROPIC",
                          "CURSOR", "GitHub"),
    ),
    Category(
        "Motor vehicle expenses", "expense", "operating", "motor_vehicle", "9281",
        cra_scrutiny_weight=2.0,
        definition="Fuel, parking, vehicle repair, vehicle insurance/registration.",
        example_keywords=("fuel", "gas station", "PARKING", "ESSO", "PETRO", "auto repair"),
    ),
    Category(
        "Other indirect expenses", "expense", "operating", "other_operating", "9270",
        is_generic=True,
        definition="Residual catch-all for legitimate operating costs fitting no line above (e.g. cleaning/janitorial).",
        example_keywords=("CLEANING", "JANITORIAL", "misc operating"),
    ),

    # ── Structural / non-P&L ──
    Category(
        "Credit card payment", "transfer", "structural", "transfer", None,
        excluded_from_pl=True,
        definition="Payment of a card balance from an own account (not an expense). Excluded from the P&L AND raw money totals (avoids double-count).",
        example_keywords=("TRSF FROM", "[CW] TF", "card balance payment"),
    ),
    Category(
        "Internal transfer", "transfer", "structural", "transfer", None,
        excluded_from_pl=True,
        definition="Own-account movement between your own accounts (USD<->CAD conversions, chequing<->card, top-ups of a payment account). Excluded from the P&L AND raw money totals (avoids double-count).",
        example_keywords=("USD TFR", "USDTFR", "[FX]USD", "TRANSFER TO SAVINGS"),
    ),
    # Owner's draw and income tax are counted as OPERATING COST everywhere
    # (analytics grouping + peer comparison) rather than as below-the-line
    # structural rows. They are grouped under Operating, and their ised_buckets
    # are dedicated no-benchmark buckets (owner_draw / income_tax) so they DO
    # count toward operating cost in the reasonableness engine, which skips only
    # transfer/non_operating/distribution. Owner's draw kind is 'expense' (not
    # 'equity') so is_distribution() does not skip it. Both remain in raw money
    # totals (neither is 'transfer').
    Category(
        "Owner's draw", "expense", "operating", "owner_draw", None,
        cra_scrutiny_weight=1.5,
        definition="Owner/shareholder distribution. Counted as an operating cost and retained in raw money totals.",
        example_keywords=("owner draw", "shareholder draw", "dividend", "personal withdrawal"),
    ),
    Category(
        "Income tax and GST/HST remittance", "tax", "operating", "income_tax", None,
        definition="Corporate income tax + sales-tax remittance to CRA. Counted as an operating cost and retained in raw money totals.",
        example_keywords=("CRA", "CANACT", "TXINS", "GST-P", "Receiver General"),
    ),
    Category(
        "Refund/Credit", "contra", "structural", None, None,
        definition="A refund/credit/chargeback reversal; signed offset netted within its originating line.",
        example_keywords=("REFUND", "DISPUTE", "chargeback reversal", "credit memo"),
    ),
)  # 27 stored categories. "Uncategorized" is NOT here — it is the NULL sentinel (see below).

assert len(ISED_CATEGORIES) == 27, "ISED_CATEGORIES must hold exactly 27 stored rows"

# ── Display-only sentinel: stored as NULL in transactions.category ──
UNCATEGORIZED_LABEL = "Uncategorized"
UNCATEGORIZED = UNCATEGORIZED_LABEL  # alias for engine imports

# ── Derived constants (build once at import) ──
JOURNAL_ONLY_NAMES: frozenset[str] = frozenset(c.name for c in ISED_CATEGORIES if c.journal_only)  # 3
CATEGORY_NAMES: tuple[str, ...] = tuple(c.name for c in ISED_CATEGORIES)          # 27, display order
CATEGORY_NAME_SET: frozenset[str] = frozenset(CATEGORY_NAMES)                     # 27 valid stored strings
ASSIGNABLE_NAME_SET: frozenset[str] = CATEGORY_NAME_SET - JOURNAL_ONLY_NAMES      # 24 picker/classifier options
CATEGORY_BY_NAME: dict[str, Category] = {c.name: c for c in ISED_CATEGORIES}
EXCLUDED_FROM_PL_NAMES: frozenset[str] = frozenset(c.name for c in ISED_CATEGORIES if c.excluded_from_pl)  # 2

_SECTION_LABELS: tuple[tuple[str, str], ...] = (
    ("revenue", "Revenue"),
    ("cost_of_sales", "Cost of sales"),
    ("operating", "Operating"),
    ("structural", "Structural"),
)

# (key, label, assignable_names) — journal-only omitted from the picker.
SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (key, label, tuple(c.name for c in ISED_CATEGORIES if c.section == key and not c.journal_only))
    for key, label in _SECTION_LABELS
)


# ── Validation / accessor helpers (the canonical accessor surface) ──
def is_valid_category(name: str | None) -> bool:
    """True iff ``name`` is a storable canonical string (27). NULL/Uncategorized validated separately."""
    return name in CATEGORY_NAME_SET


def is_assignable(name: str | None) -> bool:
    """True iff a classifier or the PATCH endpoint may assign ``name`` to a bank txn (24; excludes journal-only)."""
    return name in ASSIGNABLE_NAME_SET


def coerce_to_valid(name: str | None) -> str | None:
    """Classifier/persistence gate: return ``name`` iff assignable, else ``None`` (-> stored NULL / Uncategorized).

    This is the single accuracy gate for every LLM output: off-list OR journal-only -> None.
    """
    return name if name in ASSIGNABLE_NAME_SET else None


def ised_bucket_for(name: str | None) -> str | None:
    c = CATEGORY_BY_NAME.get(name or "")
    return c.ised_bucket if c else None


def gifi_for(name: str | None) -> str | None:
    c = CATEGORY_BY_NAME.get(name or "")
    return c.gifi if c else None


def kind_for(name: str | None) -> str:
    c = CATEGORY_BY_NAME.get(name or "")
    return c.kind if c else "none"


def section_for(name: str | None) -> str | None:
    c = CATEGORY_BY_NAME.get(name or "")
    return c.section if c else None


def is_journal_only(name: str | None) -> bool:
    return name in JOURNAL_ONLY_NAMES


def is_excluded_from_pl(name: str | None) -> bool:
    c = CATEGORY_BY_NAME.get(name or "")
    return bool(c and c.excluded_from_pl)


def is_distribution(name: str | None) -> bool:
    """Owner's-draw style equity distribution (read by reasonableness — NEVER read kind from the DB column)."""
    return kind_for(name) == "equity"


def identity_gifi_map() -> dict[str, str]:
    """name -> ised_bucket, the identity crosswalk that replaces the fuzzy aa_category_gifi_map.yaml fallback.

    Excludes rows with no benchmark line (``ised_bucket is None``: Refund/Credit),
    which fall through to the YAML fallback rules.
    """
    return {c.name: c.ised_bucket for c in ISED_CATEGORIES if c.ised_bucket}


def as_gifi_map_dict() -> dict:
    """Projection consumed by ``core.reasonableness.CategoryGifiMap``.

    Emits ``{"categories": {name: {gifi_bucket, is_operating_revenue, is_distribution,
    cra_scrutiny_weight, is_generic}}}`` for every category carrying an ``ised_bucket``
    (structural sentinels transfer/distribution/non_operating included so the engine
    skips them; Refund/Credit is omitted -> YAML 'refund' fallback -> non_operating).
    """
    categories: dict[str, dict] = {}
    for c in ISED_CATEGORIES:
        if c.ised_bucket is None:
            continue
        categories[c.name] = {
            "gifi_bucket": c.ised_bucket,
            "is_operating_revenue": c.is_operating_revenue,
            "is_distribution": c.kind == "equity",
            "cra_scrutiny_weight": c.cra_scrutiny_weight,
            "is_generic": c.is_generic,
        }
    return {"categories": categories}


def taxonomy_upsert_rows() -> list[dict]:
    """The fixed taxonomy in ``DatabasePg.upsert_user_categories`` shape (6 keys).

    Seeds all 27 stored categories with ``is_system=True`` and the rich ``kind``.
    "Uncategorized" is NOT included — it is the NULL sentinel. Consumed by the
    Phase-3 taxonomy step (``core/categorization_agent/taxonomy.py``) and by
    ``seed_fixed_taxonomy``.
    """
    return [
        {
            "name": c.name,
            "definition": c.definition,
            "umbrella_for": None,
            "example_vendors": [],
            "kind": c.kind,
            "is_system": True,
        }
        for c in ISED_CATEGORIES
    ]


def api_categories_payload() -> dict:
    """The ``GET /api/transactions/categories`` response.

    ``categories`` = flat, section-ordered list of the 24 assignable names (journal-only
    and Uncategorized omitted). ``groups`` carry full metadata for grouped rendering.
    """
    groups: list[dict] = []
    for key, label, names in SECTIONS:
        options = []
        for name in names:
            c = CATEGORY_BY_NAME[name]
            options.append({
                "name": c.name,
                "kind": c.kind,
                "section": c.section,
                "ised_bucket": c.ised_bucket,
                "gifi": c.gifi,
                "journal_only": c.journal_only,
                "excluded_from_pl": c.excluded_from_pl,
                "selectable": True,
            })
        if options:
            groups.append({"section": key, "label": label, "options": options})
    categories = [o["name"] for g in groups for o in g["options"]]
    sections = [
        {"key": key, "label": label, "order": i}
        for i, (key, label, _names) in enumerate(SECTIONS)
    ]
    return {"categories": categories, "groups": groups, "sections": sections}


def remap_legacy(name: str | None) -> str | None:
    """Normalise a stored category value against the taxonomy.

    Identity for a canonical name — journal-only ones included, which is what
    separates this from :func:`coerce_to_valid` — and ``None`` for anything
    else, which the readers render as "Uncategorized". Read paths call this
    rather than trusting the column, so a name that drifted out of the
    taxonomy cannot reach a report as if it were a category.
    """
    if name is None:
        return None
    return name if name in CATEGORY_NAME_SET else None
