"""Amazon order CSV parser — auto-detects Amazon Business and Privacy Central formats.

Parses Amazon order CSV exports into Transaction objects for reconciliation.
Supports:
- Amazon Business Analytics reports (purchase-report-*.csv)
- Amazon Privacy Central exports (Retail.OrderHistory.1.csv)

Uses Ship Date (not Order Date) as the transaction date because Amazon charges
the card at shipment. Groups multi-item orders by Order ID + Ship Date since
items that ship together are charged together.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from core.sync_utils import stable_hash
from models.transaction import AccountType, Transaction, TransactionStatus

logger = logging.getLogger(__name__)


# ── Format Detection ─────────────────────────────────────────────────

# Known column headers for format auto-detection
_BUSINESS_MARKERS = {"Charge Identifier", "ASIN/ISBN", "GL Code", "Cost Center", "Cost Centre", "Item Net Total"}
_PRIVACY_MARKERS = {"ASIN", "Shipment Item Subtotal", "Payment Instrument Type", "Total Owed"}

# Required columns per format
_BUSINESS_REQUIRED = {"Order Date", "Order ID", "Total"}
_PRIVACY_REQUIRED = {"Order ID", "Order Date", "Total Owed"}


class AmazonFormat:
    BUSINESS = "business"
    PRIVACY = "privacy"
    UNKNOWN = "unknown"


@dataclass
class ParsedOrder:
    """A single Amazon order line item."""
    order_id: str
    order_date: date | None
    ship_date: date | None
    asin: str
    product_name: str
    quantity: int
    unit_price: Decimal
    tax: Decimal
    total: Decimal
    currency: str
    status: str
    payment_method: str


@dataclass
class AmazonParseResult:
    """Result of parsing an Amazon CSV file."""
    format: str  # "business" | "privacy"
    orders: list[ParsedOrder]
    transactions: list[Transaction]
    date_range: tuple[date | None, date | None]
    imported: int
    skipped: int
    errors: list[str]


def detect_format(header_row: list[str]) -> str:
    """Detect CSV format from header columns."""
    header_set = set(h.strip() for h in header_row)

    # Check for Business-specific columns
    if header_set & _BUSINESS_MARKERS:
        return AmazonFormat.BUSINESS

    # Check for Privacy Central-specific columns
    if header_set & _PRIVACY_MARKERS:
        return AmazonFormat.PRIVACY

    # Fallback: check required columns
    if _BUSINESS_REQUIRED.issubset(header_set):
        return AmazonFormat.BUSINESS
    if _PRIVACY_REQUIRED.issubset(header_set):
        return AmazonFormat.PRIVACY

    return AmazonFormat.UNKNOWN


# ── Date Parsing ─────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%d",    # 2000-01-15
    "%Y/%m/%d",    # 2026/01/15 (Amazon Business Report)
    "%m/%d/%Y",    # 01/15/2026
    "%m/%d/%y",    # 01/15/26
    "%d/%m/%Y",    # 15/01/2026 (less common)
    "%B %d, %Y",   # January 15, 2026
    "%b %d, %Y",   # Jan 15, 2026
]


def _parse_date(s: str | None) -> date | None:
    """Try multiple date formats. Return None on failure."""
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(s: str | None) -> Decimal:
    """Parse a decimal value from CSV. Handles $, commas, empty strings."""
    if not s or not s.strip():
        return Decimal("0")
    s = s.strip().replace("$", "").replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


# ── CSV Parsing ──────────────────────────────────────────────────────

def _parse_privacy_csv(reader: csv.DictReader) -> tuple[list[ParsedOrder], list[str]]:
    """Parse Amazon Privacy Central CSV (Retail.OrderHistory.1.csv)."""
    orders: list[ParsedOrder] = []
    errors: list[str] = []

    for i, row in enumerate(reader, start=2):  # Row 2+ (1-indexed, header is row 1)
        try:
            order_id = (row.get("Order ID") or "").strip()
            if not order_id:
                continue

            # Skip cancelled orders
            status = (row.get("Order Status") or "").strip()
            if status.lower() in ("cancelled", "canceled"):
                continue

            order_date = _parse_date(row.get("Order Date"))
            ship_date = _parse_date(row.get("Ship Date"))
            asin = (row.get("ASIN") or "").strip()
            product_name = (row.get("Product Name") or (row.get("Title") or "")).strip()
            quantity = int(row.get("Quantity") or "1")
            unit_price = _parse_decimal(row.get("Unit Price"))
            tax = _parse_decimal(row.get("Unit Price Tax") or row.get("Shipment Item Subtotal Tax"))
            total = _parse_decimal(row.get("Total Owed") or row.get("Shipment Item Subtotal"))
            currency = (row.get("Currency") or "CAD").strip().upper()
            payment_method = (row.get("Payment Instrument Type") or "").strip()

            # If total is 0, compute from unit_price * qty + tax
            if total == Decimal("0") and unit_price > 0:
                total = unit_price * quantity + tax

            orders.append(ParsedOrder(
                order_id=order_id,
                order_date=order_date,
                ship_date=ship_date,
                asin=asin,
                product_name=product_name,
                quantity=quantity,
                unit_price=unit_price,
                tax=tax,
                total=total,
                currency=currency,
                status=status,
                payment_method=payment_method,
            ))
        except Exception as e:
            errors.append(f"Row {i}: {e}")

    return orders, errors


def _parse_business_csv(reader: csv.DictReader) -> tuple[list[ParsedOrder], list[str]]:
    """Parse Amazon Business Analytics CSV."""
    orders: list[ParsedOrder] = []
    errors: list[str] = []

    for i, row in enumerate(reader, start=2):
        try:
            order_id = (row.get("Order ID") or "").strip()
            if not order_id:
                continue

            # Skip cancelled/returned orders
            status = (row.get("Order Status") or "").strip()
            if status.lower() in ("cancelled", "canceled"):
                continue

            order_date = _parse_date(row.get("Order Date"))
            ship_date = _parse_date(
                row.get("Ship Date")
                or row.get("Invoice Date")
                or row.get("Payment Date")
                or row.get("Invoice Issue Date")
            )
            asin = (row.get("ASIN/ISBN") or row.get("ASIN") or "").strip()
            product_name = (row.get("Product Name") or row.get("Title") or "").strip()
            quantity = int(row.get("Quantity") or row.get("Item Quantity") or row.get("Order Quantity") or "1")
            unit_price = _parse_decimal(
                row.get("Unit Price")
                or row.get("Price")
                or row.get("Purchase PPU")
                or row.get("Item Subtotal")
            )
            # Tax: try single column first, then sum federal + provincial
            tax_raw = row.get("Tax") or row.get("Tax Amount") or row.get("Unit Price Tax")
            if tax_raw:
                tax = _parse_decimal(tax_raw)
            else:
                tax = (
                    _parse_decimal(row.get("Item Federal Tax"))
                    + _parse_decimal(row.get("Item Provincial Tax"))
                    + _parse_decimal(row.get("Item Regulatory Fee"))
                )
            total = _parse_decimal(
                row.get("Total")
                or row.get("Amount")
                or row.get("Item Net Total")
                or row.get("Order net total")
            )
            currency = (row.get("Currency") or "CAD").strip().upper()
            payment_method = (row.get("Payment Method") or row.get("Payment Instrument Type") or "").strip()

            if total == Decimal("0") and unit_price > 0:
                total = unit_price * quantity + tax

            orders.append(ParsedOrder(
                order_id=order_id,
                order_date=order_date,
                ship_date=ship_date,
                asin=asin,
                product_name=product_name,
                quantity=quantity,
                unit_price=unit_price,
                tax=tax,
                total=total,
                currency=currency,
                status=status,
                payment_method=payment_method,
            ))
        except Exception as e:
            errors.append(f"Row {i}: {e}")

    return orders, errors


# ── Grouping & Transaction Mapping ───────────────────────────────────

def _dedup_key(order: ParsedOrder) -> str:
    """Generate a unique dedup key for an order line item."""
    ship = order.ship_date.isoformat() if order.ship_date else "noship"
    return f"{order.order_id}_{order.asin}_{ship}"


def _group_into_shipments(orders: list[ParsedOrder]) -> list[tuple[str, list[ParsedOrder]]]:
    """Group orders by Order ID + Ship Date (items shipping together are charged together)."""
    groups: dict[str, list[ParsedOrder]] = {}
    for order in orders:
        ship = order.ship_date.isoformat() if order.ship_date else "noship"
        key = f"{order.order_id}_{ship}"
        groups.setdefault(key, []).append(order)
    return list(groups.items())


def _orders_to_transactions(
    orders: list[ParsedOrder],
    source_file: str,
) -> list[Transaction]:
    """Convert parsed orders to Transaction objects grouped by shipment."""
    shipments = _group_into_shipments(orders)
    transactions: list[Transaction] = []

    for group_key, items in shipments:
        # Use ship date if available, otherwise order date, otherwise today
        txn_date = items[0].ship_date or items[0].order_date or date.today()
        currency = items[0].currency
        order_id = items[0].order_id

        # Sum totals for the shipment
        total = sum(item.total for item in items)
        tax_total = sum(item.tax for item in items)

        # Build description from item names (truncate if too long)
        if len(items) == 1:
            desc = f"Amazon: {items[0].product_name}"
        else:
            names = [item.product_name for item in items]
            desc = f"Amazon ({len(items)} items): {', '.join(names)}"
        if len(desc) > 500:
            desc = desc[:497] + "..."

        # Determine account type based on currency
        if currency == "USD":
            account = AccountType.AMAZON
        else:
            account = AccountType.AMAZON

        # Generate stable dedup hash
        source_row = stable_hash(group_key)

        # All Amazon purchases are debits (expenses)
        txn_type = "DEBIT"
        # Handle returns/refunds (negative total)
        if total < 0:
            txn_type = "CREDIT"
            total = abs(total)

        transactions.append(Transaction(
            account=account,
            transaction_type=txn_type,
            date_posted=txn_date,
            amount=total,
            currency=currency if currency in ("CAD", "USD") else "CAD",
            description=desc,
            source_file=source_file,
            source_row=source_row,
            status=TransactionStatus.UNMATCHED,
            category="Office supplies",  # Default; LLM categorization will refine
            note=f"Order {order_id}" + (f" | Tax: ${tax_total}" if tax_total > 0 else ""),
        ))

    return transactions


# ── Public API ───────────────────────────────────────────────────────

def preview_csv(content: str) -> dict:
    """Preview a CSV file: detect format, count orders, extract date range.

    Returns dict with format, order_count, date_range, sample_orders.
    Used by the /api/onboarding/amazon/preview endpoint.
    """
    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader)
    except StopIteration:
        return {"format": "unknown", "order_count": 0, "date_range": None, "error": "Empty file"}

    fmt = detect_format(header)
    if fmt == AmazonFormat.UNKNOWN:
        return {"format": "unknown", "order_count": 0, "date_range": None, "error": "Unrecognized CSV format"}

    # Re-parse with DictReader
    dict_reader = csv.DictReader(io.StringIO(content))
    if fmt == AmazonFormat.BUSINESS:
        orders, errors = _parse_business_csv(dict_reader)
    else:
        orders, errors = _parse_privacy_csv(dict_reader)

    # Extract date range
    dates = [o.ship_date or o.order_date for o in orders if o.ship_date or o.order_date]
    date_range = None
    if dates:
        date_range = {
            "from": min(dates).isoformat(),
            "to": max(dates).isoformat(),
        }

    # Sample orders for preview
    sample = []
    for o in orders[:5]:
        sample.append({
            "order_id": o.order_id,
            "date": (o.ship_date or o.order_date or "").isoformat() if (o.ship_date or o.order_date) else None,
            "product": o.product_name[:80],
            "total": str(o.total),
            "currency": o.currency,
        })

    return {
        "format": "Amazon Business Report" if fmt == AmazonFormat.BUSINESS else "Amazon Order History",
        "order_count": len(orders),
        "date_range": date_range,
        "sample_orders": sample,
        "errors": errors[:5] if errors else [],
    }


def parse_csv(content: str, source_file: str) -> AmazonParseResult:
    """Parse an Amazon CSV file and return Transaction objects.

    Args:
        content: Raw CSV text content
        source_file: Source file identifier for transactions (e.g. "amazon_import_20260410_143000")

    Returns:
        AmazonParseResult with parsed transactions and metadata
    """
    # Strip BOM if present (common in Amazon Business exports)
    content = content.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader)
    except StopIteration:
        return AmazonParseResult(
            format=AmazonFormat.UNKNOWN,
            orders=[],
            transactions=[],
            date_range=(None, None),
            imported=0,
            skipped=0,
            errors=["Empty CSV file"],
        )

    fmt = detect_format(header)
    if fmt == AmazonFormat.UNKNOWN:
        return AmazonParseResult(
            format=AmazonFormat.UNKNOWN,
            orders=[],
            transactions=[],
            date_range=(None, None),
            imported=0,
            skipped=0,
            errors=["Unrecognized CSV format. Expected Amazon Business Analytics or Privacy Central export."],
        )

    # Parse orders
    dict_reader = csv.DictReader(io.StringIO(content))
    if fmt == AmazonFormat.BUSINESS:
        orders, errors = _parse_business_csv(dict_reader)
    else:
        orders, errors = _parse_privacy_csv(dict_reader)

    if not orders:
        return AmazonParseResult(
            format=fmt,
            orders=[],
            transactions=[],
            date_range=(None, None),
            imported=0,
            skipped=0,
            errors=errors or ["No orders found in CSV"],
        )

    # Convert to transactions
    transactions = _orders_to_transactions(orders, source_file)

    # Date range
    dates = [o.ship_date or o.order_date for o in orders if o.ship_date or o.order_date]
    date_range = (min(dates), max(dates)) if dates else (None, None)

    return AmazonParseResult(
        format=fmt,
        orders=orders,
        transactions=transactions,
        date_range=date_range,
        imported=len(transactions),
        skipped=0,
        errors=errors,
    )
