"""One rule for comparing a bank amount to a proof-of-transaction amount.

Answering this question in more than one place lets the same pair get more than
one answer: a CAD 2,500 bank row against a USD 2,500 receipt can be *rejected*
("outside the 15% tolerance"), scored *confident*, and labelled *"Exact amount
match"* all at once — one of those being a false statement shown to a user.

The rule, stated once:

* **Same currency** — compare magnitudes. Equal is ``EXACT``; within
  :data:`PENNY_TOLERANCE` is ``ROUNDING``; within the caller's tolerance is
  ``WITHIN_TOLERANCE``; anything else is ``MISMATCH``.
* **Different currencies** — never compare magnitudes, and never call the pair
  exact. Convert: the implied rate has to land inside the CAD/USD band defined
  here (:data:`FX_CAD_USD_MIN` / :data:`FX_CAD_USD_MAX`, env-tunable) to be an
  ``FX_BAND`` match. Outside the band it is a ``MISMATCH``; for a currency pair
  with no band configured it is ``NO_FX_BAND`` — unknown, not matched. These two
  constants are the single definition of the band: every other module in the
  engine imports them rather than restating a range.
* **A currency the receipt never stated** — read it in the bank line's
  currency and compare magnitudes, verdict ``ASSUMED_SAME_CURRENCY``, and say
  so in the sentence. A store-receipt screenshot printing a bare ``$`` that the
  extractor guesses as USD would otherwise score 0 against the CAD charge it is
  proof of; "different currency" is a claim, and the engine makes it only when
  the document made it first. :mod:`core.document_currency` decides which case
  a document is in.

Callers get a verdict, a score, and :meth:`AmountComparison.phrase` — the
user-facing sentence — from the same object, so the number a user sees and the
sentence explaining it can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from config.settings import reconciliation_config as _recon_cfg
from core.document_currency import (
    ASSUMED,
    INFERRED,
    STATED,
    normalize_currency_code,
    normalize_currency_source,
    resolve_document_currency,
)

# ── The CAD/USD band ──────────────────────────────────────────────────────
# Canonical constants (env: FX_CAD_USD_MIN / FX_CAD_USD_MAX). The band brackets
# the plausible CAD/USD conversion range: an implied rate outside it is not a
# conversion, it is a coincidence of digits. A ledger kept in a different
# currency pair widens or replaces the band through the environment. Every other
# module — matcher, validator, explainer, prompt text — imports these rather than
# writing a range of its own, so there is one band to change.
FX_CAD_USD_MIN = Decimal(str(_recon_cfg.fx_cad_usd_min))
FX_CAD_USD_MAX = Decimal(str(_recon_cfg.fx_cad_usd_max))

# Penny rounding — FX settlement and card networks round the last cent.
PENNY_TOLERANCE = Decimal("0.05")

# The wire-fee allowance (env: WIRE_FEE_TOLERANCE). A bank that takes its
# handling fee out of an incoming wire credits less than the invoice by that
# fee, so a difference no larger than this is still the same payment. Defined
# here with the band so the matcher, the post-match validator and the prompt
# text that describes either of them all read one number.
WIRE_FEE_TOLERANCE = Decimal(str(_recon_cfg.wire_fee_tolerance))

# Score for a cross-currency pair whose implied rate is inside the band.
# Flat on purpose: any rate in the band is equally plausible, and a score that
# peaks at the band's midpoint biases the greedy matcher into cross-assigning
# same-date payments through the same provider.
FX_BAND_SCORE = 0.78

# Verdicts
EXACT = "exact"
ROUNDING = "rounding"
WITHIN_TOLERANCE = "within_tolerance"
FX_BAND = "fx_band"
MISMATCH = "mismatch"
NO_FX_BAND = "no_fx_band"
UNKNOWN = "unknown"
# The receipt never said which currency it was in, so it was read in the
# transaction's. Scores exactly as the same-currency comparison it is; carries
# its own verdict so no surface can call it a plain "exact match" in silence.
ASSUMED_SAME_CURRENCY = "assumed_same_currency"


def _fmt(amount: Decimal, currency: str | None = None) -> str:
    try:
        text = f"${abs(amount):,.2f}"
    except Exception:
        text = f"${amount}"
    return f"{text} {currency}" if currency else text


@dataclass(frozen=True)
class AmountComparison:
    """The verdict on one (bank amount, document total) pair."""

    verdict: str
    score: float
    same_currency: bool
    txn_amount: Decimal
    txn_currency: str
    doc_total: Decimal | None
    doc_currency: str
    diff: Decimal | None = None
    implied_rate: Decimal | None = None
    # Where the document's currency came from: ``stated`` (the document says
    # so), ``inferred`` (read off its Canadian tax line), ``assumed`` (nothing
    # said — ``doc_currency`` is then the transaction's, and the sentence says
    # as much).
    doc_currency_source: str = STATED
    # For an assumed currency: the same-currency verdict the magnitudes earned,
    # kept so the sentence can still say *exactly* / *to the penny*.
    magnitude_verdict: str | None = None

    @property
    def is_exact(self) -> bool:
        """True only for a same-currency, to-the-cent equal pair.

        The one question the "Exact amount match" wording is allowed to ask —
        and an assumed currency is not allowed to ask it unqualified, which is
        why ``ASSUMED_SAME_CURRENCY`` is a verdict of its own.
        """
        return self.verdict == EXACT

    @property
    def is_cross_currency(self) -> bool:
        return not self.same_currency

    @property
    def currency_assumed(self) -> bool:
        return self.doc_currency_source == ASSUMED

    @property
    def currency_inferred(self) -> bool:
        return self.doc_currency_source == INFERRED

    @property
    def matched(self) -> bool:
        return self.score > 0.0

    def currency_note(self) -> str:
        """The clause naming where the receipt's currency came from, if at all.

        Empty for a document that stated its own currency, so a sentence about
        such a pair gains no extra words.
        """
        if self.doc_total is None:
            return ""
        if self.currency_assumed:
            read_as = self.txn_currency or "CAD"
            return f"Receipt does not state a currency; read as {read_as}."
        if self.currency_inferred and self.same_currency:
            return (
                f"Receipt currency inferred as {self.doc_currency} from its "
                f"Canadian tax line."
            )
        return ""

    def phrase(self) -> str:
        """A user-facing sentence fragment describing the comparison."""
        note = self.currency_note()
        body = self._magnitude_phrase()
        return f"{note} {body}" if note else body

    def _magnitude_phrase(self) -> str:
        verdict = self.magnitude_verdict or self.verdict
        if verdict == UNKNOWN:
            return "No amount on the proof of transaction"
        if verdict == EXACT:
            return f"Exact amount match ({_fmt(self.txn_amount, self.txn_currency)})"
        if verdict == ROUNDING:
            return (
                f"Amounts agree to the penny "
                f"({_fmt(self.txn_amount)} vs {_fmt(self.doc_total)}, "
                f"{_fmt(self.diff or Decimal(0))} rounding)"
            )
        if verdict == WITHIN_TOLERANCE:
            return (
                f"Amounts close ({_fmt(self.txn_amount)} vs {_fmt(self.doc_total)}, "
                f"{_fmt(self.diff or Decimal(0))} apart)"
            )
        if verdict == FX_BAND:
            return (
                f"FX match ({_fmt(self.txn_amount, self.txn_currency)} vs "
                f"{_fmt(self.doc_total, self.doc_currency)}, implied rate "
                f"{self.implied_rate:.4f} within {FX_CAD_USD_MIN:.2f}-{FX_CAD_USD_MAX:.2f})"
            )
        if verdict == NO_FX_BAND:
            return (
                f"Cross-currency amounts not comparable — no FX band for "
                f"{self.txn_currency or '?'} vs {self.doc_currency or '?'} "
                f"({_fmt(self.txn_amount, self.txn_currency)} vs "
                f"{_fmt(self.doc_total, self.doc_currency)})"
            )
        # MISMATCH
        if self.same_currency:
            return (
                f"Amount mismatch ({_fmt(self.txn_amount)} vs "
                f"{_fmt(self.doc_total)})"
            )
        rate = f", implied rate {self.implied_rate:.4f}" if self.implied_rate else ""
        return (
            f"Cross-currency amounts do not reconcile at any plausible "
            f"{FX_CAD_USD_MIN:.2f}-{FX_CAD_USD_MAX:.2f} rate "
            f"({_fmt(self.txn_amount, self.txn_currency)} vs "
            f"{_fmt(self.doc_total, self.doc_currency)}{rate})"
        )


def compare_amounts(
    txn_amount: Decimal,
    txn_currency: str | None,
    doc_total: Decimal | None,
    doc_currency: str | None,
    tolerance: Decimal = PENNY_TOLERANCE,
    *,
    doc_currency_source: str | None = None,
) -> AmountComparison:
    """Compare a bank amount to a document total under the one rule.

    ``tolerance`` widens only the *same-currency* comparison (a relaxed pass
    passes a dollar or two). It never widens a cross-currency comparison —
    there, the FX band is the only tolerance there is.

    ``doc_currency_source`` is the provenance of ``doc_currency``, from
    :func:`core.document_currency.resolve_document_currency`: ``stated``,
    ``inferred`` or ``assumed``. Omitted, provenance is derived from the code
    itself — a well-formed code is a reading of the document, anything else
    (``None``, ``""``, ``"$"``) is no reading at all and the receipt gets
    compared in the transaction's own currency.
    """
    txn_cur = (txn_currency or "").upper()
    code = normalize_currency_code(doc_currency)
    if doc_currency_source is None:
        source = STATED if code else ASSUMED
    else:
        source = normalize_currency_source(doc_currency_source) or (
            STATED if code else ASSUMED
        )
    if source == ASSUMED:
        # An assumed currency is not a currency. Whatever token came with it is
        # not evidence of anything, so it is not compared against.
        code = None
    # The currency the receipt is *read in*: its own when it has one, the bank
    # line's when it does not.
    doc_cur = code or txn_cur
    same_currency = code is None or txn_cur == code

    if doc_total is None:
        return AmountComparison(
            verdict=UNKNOWN, score=0.0, same_currency=same_currency,
            txn_amount=txn_amount, txn_currency=txn_cur,
            doc_total=None, doc_currency=doc_cur,
            doc_currency_source=source,
        )

    txn_abs = abs(txn_amount)
    doc_abs = abs(doc_total)

    if same_currency:
        diff = abs(txn_abs - doc_abs)
        if diff == 0:
            verdict, score = EXACT, 1.0
        elif diff <= PENNY_TOLERANCE:
            verdict, score = ROUNDING, 0.9
        elif diff <= tolerance:
            verdict, score = WITHIN_TOLERANCE, 0.8
        else:
            verdict, score = MISMATCH, 0.0
        magnitude_verdict = None
        if source == ASSUMED and verdict != MISMATCH:
            # Same figure, but the engine chose the currency it is read in.
            # Score it exactly as the same-currency pair it is, and name the
            # assumption.
            magnitude_verdict, verdict = verdict, ASSUMED_SAME_CURRENCY
        return AmountComparison(
            verdict=verdict, score=score, same_currency=True,
            txn_amount=txn_amount, txn_currency=txn_cur,
            doc_total=doc_total, doc_currency=doc_cur, diff=diff,
            doc_currency_source=source, magnitude_verdict=magnitude_verdict,
        )

    # ── Cross-currency: convert, never compare magnitudes ──
    if txn_abs == 0 or doc_abs == 0:
        return AmountComparison(
            verdict=MISMATCH, score=0.0, same_currency=False,
            txn_amount=txn_amount, txn_currency=txn_cur,
            doc_total=doc_total, doc_currency=doc_cur,
            doc_currency_source=source,
        )

    if txn_cur == "CAD" and doc_cur == "USD":
        implied_rate = txn_abs / doc_abs
    elif txn_cur == "USD" and doc_cur == "CAD":
        implied_rate = doc_abs / txn_abs
    else:
        # A band is configured for CAD/USD only. Anything else is unknown, and
        # unknown is never a match: uncertain beats wrong.
        return AmountComparison(
            verdict=NO_FX_BAND, score=0.0, same_currency=False,
            txn_amount=txn_amount, txn_currency=txn_cur,
            doc_total=doc_total, doc_currency=doc_cur,
            doc_currency_source=source,
        )

    in_band = FX_CAD_USD_MIN <= implied_rate <= FX_CAD_USD_MAX
    return AmountComparison(
        verdict=FX_BAND if in_band else MISMATCH,
        score=FX_BAND_SCORE if in_band else 0.0,
        same_currency=False,
        txn_amount=txn_amount, txn_currency=txn_cur,
        doc_total=doc_total, doc_currency=doc_cur,
        implied_rate=implied_rate,
        doc_currency_source=source,
    )


def compare_amounts_for_document(
    txn_amount: Decimal,
    txn_currency: str | None,
    doc_total: Decimal | None,
    doc,
    tolerance: Decimal = PENNY_TOLERANCE,
) -> AmountComparison:
    """:func:`compare_amounts` with the document's currency resolved first.

    ``doc`` is anything :func:`core.document_currency.resolve_document_currency`
    reads — a ``Document``, a ``documents`` row as a dict, an extraction
    payload. ``doc_total`` stays a parameter because callers sometimes compare
    against an alternate amount printed on the same receipt.
    """
    code, source = resolve_document_currency(doc)
    return compare_amounts(
        txn_amount, txn_currency, doc_total, code, tolerance,
        doc_currency_source=source,
    )
