"""Bank CSV parser module.

Parses BMO CAD/USD, BMO Credit Card, Wise, Amazon, and PayPal CSV files
into Transaction objects.
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from models.transaction import AccountType, Transaction

logger = logging.getLogger(__name__)


def _first_data_line(file_path: Path) -> str:
    """Return the first non-empty, non-comment line from a CSV file.

    Tries UTF-8 first, falls back to latin-1 for files with non-UTF-8
    characters (common in PayPal / Amazon exports).
    """
    for enc in ("utf-8", "latin-1"):
        try:
            with open(file_path, encoding=enc) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        return stripped
            return ""
        except UnicodeDecodeError:
            continue
    return ""


def detect_csv_format(file_path: Path) -> str:
    """Detect CSV format from the header row.

    Returns one of: "bmo_cad", "bmo_credit_card", "wise", "amazon", "paypal".
    Raises ValueError for unrecognised formats.
    Skips leading comment lines (starting with ``#``) and blank lines.
    """
    header = _first_data_line(file_path)

    if "Transaction Type" in header and "Date Posted" in header and "Transaction Amount" in header:
        return "bmo_cad"
    if "Posting Date" in header and "Card Number" in header:
        return "bmo_credit_card"
    if "ID" in header and "Status" in header and "Direction" in header and "Exchange rate" in header:
        return "wise"
    if "TimeZone" in header and "Gross" in header and "Net" in header:
        return "paypal"

    # Amazon Privacy Central: ASIN, Total Owed, Payment Instrument Type
    if "ASIN" in header and "Total Owed" in header and "Payment Instrument Type" in header:
        return "amazon"
    # Amazon Business: ASIN/ISBN, Charge Identifier
    if "ASIN/ISBN" in header and "Charge Identifier" in header:
        return "amazon"
    # Amazon Business Report (Analytics export): ASIN + Item Net Total
    if "ASIN" in header and "Item Net Total" in header:
        return "amazon"

    # PayPal CSV download: Date, Time, TimeZone, Name, Type, Status, Currency, Gross, ...
    if "Transaction ID" in header and "Gross" in header and "Currency" in header:
        return "paypal"

    raise ValueError("Unknown or unrecognized CSV format")


def _skip_to_header(f) -> csv.reader:
    """Advance the file past any leading comment/blank lines, then return a
    csv.reader positioned after the header row."""
    for line in f:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break  # this was the header line — now the reader starts on data
    return csv.reader(f)


def parse_bmo_csv(file_path: Path, account: AccountType) -> list[Transaction]:
    """Parse a BMO CAD or USD CSV file into Transaction objects."""
    currency = "CAD" if account == AccountType.CAD else "USD"
    transactions: list[Transaction] = []

    with open(file_path, encoding="utf-8", newline="") as f:
        reader = _skip_to_header(f)

        for row_idx, row in enumerate(reader, start=1):
            if not row or (len(row) == 1 and row[0].strip().startswith("#")):
                continue  # skip inline comment / blank lines
            if len(row) < 5:
                logger.warning("Skipping malformed row %d: fewer than 5 columns", row_idx)
                continue

            _account_number, transaction_type, date_str, amount_str, description = (
                row[0], row[1], row[2], row[3], row[4]
            )

            date_posted = datetime.strptime(date_str, "%Y%m%d").date()

            transactions.append(Transaction(
                account=account,
                transaction_type=transaction_type,
                date_posted=date_posted,
                amount=Decimal(amount_str),
                currency=currency,
                description=description,
                source_file=str(file_path),
                source_row=row_idx,
            ))

    return transactions


def parse_bmo_credit_card_csv(file_path: Path) -> list[Transaction]:
    """Parse a BMO Credit Card CSV file into Transaction objects."""
    transactions: list[Transaction] = []

    with open(file_path, encoding="utf-8", newline="") as f:
        reader = _skip_to_header(f)

        for row_idx, row in enumerate(reader, start=1):
            if not row or (len(row) == 1 and row[0].strip().startswith("#")):
                continue
            if len(row) < 5:
                logger.warning("Skipping malformed row %d: fewer than 5 columns", row_idx)
                continue

            _txn_date, posting_date_str, description, amount_str, _card_number = (
                row[0], row[1], row[2], row[3], row[4]
            )

            date_posted = datetime.strptime(posting_date_str, "%m/%d/%Y").date()
            amount = Decimal(amount_str)
            # Card-statement convention: positive = DEBIT (purchase/charge),
            # negative = CREDIT (payment/refund to card)
            transaction_type = "CREDIT" if amount < 0 else "DEBIT"

            transactions.append(Transaction(
                account=AccountType.CREDIT_CARD,
                transaction_type=transaction_type,
                date_posted=date_posted,
                amount=amount,
                currency="CAD",
                description=description,
                source_file=str(file_path),
                source_row=row_idx,
            ))

    return transactions


def parse_wise_csv(file_path: Path) -> list[Transaction]:
    """Parse a Wise CSV file into Transaction objects.

    Only COMPLETED transactions are included.
    """
    transactions: list[Transaction] = []

    with open(file_path, encoding="utf-8", newline="") as f:
        reader = _skip_to_header(f)

        for row_idx, row in enumerate(reader, start=1):
            if not row or (len(row) == 1 and row[0].strip().startswith("#")):
                continue
            status = row[1]
            if status != "COMPLETED":
                continue

            direction = row[2]
            created_on_str = row[3]
            source_amount = Decimal(row[5])
            source_currency = row[6]
            target_amount = row[7]
            target_currency = row[8]
            exchange_rate = row[9]
            reference = row[11]

            date_posted = datetime.strptime(created_on_str, "%Y-%m-%d %H:%M:%S").date()

            amount = -source_amount if direction == "DEBIT" else source_amount

            # Build description with reference and exchange info
            desc_parts = [reference]
            if target_currency and exchange_rate:
                desc_parts.append(f"({source_currency} -> {target_amount} {target_currency} @ {exchange_rate})")
            description = " ".join(desc_parts)

            transactions.append(Transaction(
                account=AccountType.WISE,
                transaction_type=direction,
                date_posted=date_posted,
                amount=amount,
                currency=source_currency,
                description=description,
                source_file=str(file_path),
                source_row=row_idx,
            ))

    return transactions


_SUPPORTED_CURRENCIES = {"CAD", "USD", "CNY", "EUR", "GBP"}

# PayPal transaction types that represent internal/non-balance transfers
# and should be excluded to avoid duplicates during reconciliation.
_PAYPAL_SKIP_TYPES = {
    "currency conversion",
    "general currency conversion",
    "hold on balance for dispute investigation",
    "release of hold",
    "authorization",
    "order",
}


def _parse_paypal_date(date_str: str) -> date | None:
    """Parse a PayPal CSV date, handling MM/DD/YYYY, DD/MM/YYYY, and ISO.

    Heuristic for slash-separated dates:
    - First segment > 12 → must be day → DD/MM/YYYY
    - Second segment > 12 → must be day → MM/DD/YYYY
    - Both ≤ 12 (ambiguous) → defaults to MM/DD/YYYY (PayPal default for
      most English-language accounts)
    """
    date_str = date_str.strip().strip('"')
    if not date_str:
        return None

    # ISO format (YYYY-MM-DD)
    if "-" in date_str and len(date_str) == 10 and date_str[4] == "-":
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    # Slash-separated: use heuristic to pick the right format
    parts = date_str.split("/")
    if len(parts) == 3:
        try:
            a = int(parts[0])
            int(parts[1])  # validate second segment is numeric
        except ValueError:
            logger.warning("Could not parse PayPal date: %s", date_str)
            return None

        if a > 12:
            # First segment can't be a month → DD/MM/YYYY
            fmt = "%d/%m/%Y"
        else:
            # Default to MM/DD/YYYY (covers both unambiguous and ambiguous)
            fmt = "%m/%d/%Y"
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            pass

    logger.warning("Could not parse PayPal date: %s", date_str)
    return None


def _clean_amount(s: str) -> Decimal:
    """Parse a PayPal amount string like '1,234.56' or '-3.50' into Decimal."""
    s = s.strip().strip('"').replace(",", "")
    if not s:
        return Decimal("0")
    return Decimal(s)


def parse_amazon_csv(file_path: Path) -> list[Transaction]:
    """Parse an Amazon CSV file (Business or Privacy Central) into Transaction objects.

    Delegates to the dedicated Amazon parser in core.amazon_parser.
    """
    from core.amazon_parser import parse_csv as amazon_parse

    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(file_path, encoding=enc) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"Cannot decode Amazon CSV file: {file_path}")

    result = amazon_parse(content, file_path.name)
    return result.transactions


def parse_paypal_csv(file_path: Path) -> list[Transaction]:
    """Parse a PayPal Activity Download CSV into Transaction objects.

    Only Completed transactions with supported currencies are imported.
    Internal transfers (currency conversions, holds) are skipped.
    """
    transactions: list[Transaction] = []

    with open(file_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ValueError("PayPal CSV has no header row")

        # Normalize headers — PayPal sometimes includes quotes/spaces
        reader.fieldnames = [h.strip().strip('"') for h in reader.fieldnames]

        for row_idx, row in enumerate(reader, start=2):  # row 1 is header
            status = (row.get("Status") or "").strip()
            if status != "Completed":
                continue

            txn_type = (row.get("Type") or "").strip()
            if txn_type.lower() in _PAYPAL_SKIP_TYPES:
                continue

            currency = (row.get("Currency") or "").strip().upper()
            if currency not in _SUPPORTED_CURRENCIES:
                logger.info(
                    "Skipping PayPal row %d: unsupported currency %s",
                    row_idx, currency,
                )
                continue

            date_str = (row.get("Date") or "").strip()
            date_posted = _parse_paypal_date(date_str)
            if date_posted is None:
                logger.warning("Skipping PayPal row %d: unparseable date '%s'", row_idx, date_str)
                continue

            gross = _clean_amount(row.get("Gross") or "0")
            if gross == 0:
                continue

            fee = _clean_amount(row.get("Fee") or "0")
            name = (row.get("Name") or "").strip()
            subject = (row.get("Subject") or "").strip()

            # Build description from name + subject + type
            desc_parts = [name] if name else []
            if subject and subject != name:
                desc_parts.append(subject)
            if not desc_parts:
                desc_parts.append(txn_type)
            if fee and fee != Decimal("0"):
                desc_parts.append(f"(fee: {fee})")
            description = " — ".join(desc_parts)

            transaction_type = "CREDIT" if gross > 0 else "DEBIT"

            transactions.append(Transaction(
                account=AccountType.PAYPAL,
                transaction_type=transaction_type,
                date_posted=date_posted,
                amount=gross,
                currency=currency,
                description=description,
                source_file=str(file_path),
                source_row=row_idx,
            ))

    logger.info("Parsed %d PayPal transactions from %s", len(transactions), file_path.name)
    return transactions


def _infer_account_from_filename(file_path: Path) -> AccountType | None:
    """Try to infer account type from the filename.

    Recognises patterns like:
      "Jan2026_USD.csv", "Dec2026_CreditCard.csv", "bmo_cad_jan.csv"
    """
    name = file_path.stem.lower()
    if "creditcard" in name or "credit_card" in name or "cc" in name.split("_"):
        return AccountType.CREDIT_CARD
    if "_usd" in name or "usd_" in name or name.endswith("usd"):
        return AccountType.USD
    if "_cad" in name or "cad_" in name or name.endswith("cad"):
        return AccountType.CAD
    return None


def parse_csv(file_path: Path, account_override: AccountType | None = None) -> list[Transaction]:
    """Auto-detect CSV format and parse into Transaction objects.

    If account_override is provided, it is used instead of auto-detection
    for BMO CAD/USD format (useful when the CSV was converted from a PDF
    whose account type is already known).

    If no override is given, tries to infer the account from the filename
    (e.g., "Dec2026_USD.csv" → USD).
    """
    fmt = detect_csv_format(file_path)

    if fmt in ("bmo_cad", "bmo_usd"):
        account = account_override or _infer_account_from_filename(file_path) or AccountType.CAD
        return parse_bmo_csv(file_path, account)
    elif fmt == "bmo_credit_card":
        return parse_bmo_credit_card_csv(file_path)
    elif fmt == "wise":
        return parse_wise_csv(file_path)
    elif fmt == "amazon":
        return parse_amazon_csv(file_path)
    elif fmt == "paypal":
        return parse_paypal_csv(file_path)
    else:
        raise ValueError(f"Unsupported format: {fmt}")
