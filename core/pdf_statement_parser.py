"""Parse bank statement PDFs into CSV format for the bank parser.

BMO and other banks often provide statements as PDFs rather than CSVs.
This module extracts transaction tables from PDF bank statements using
pdfplumber, then writes a temporary CSV that the existing bank_parser
can handle.
"""

from __future__ import annotations

import csv
import logging
import re
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)


def extract_text_from_pdf(file_path: Path) -> str:
    """Extract all text from a PDF file."""
    text_parts = []
    with pdfplumber.open(str(file_path)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def extract_tables_from_pdf(file_path: Path) -> list[list[list[str]]]:
    """Extract all tables from a PDF file using pdfplumber."""
    all_tables = []
    with pdfplumber.open(str(file_path)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if tables:
                all_tables.extend(tables)
    return all_tables


def is_bank_statement_pdf(file_path: Path) -> bool:
    """Check if a PDF looks like a bank statement (has transaction data)."""
    try:
        text = extract_text_from_pdf(file_path)
        text_lower = text.lower()
        # Look for common bank statement indicators
        indicators = [
            "transaction", "balance", "deposit", "withdrawal",
            "debit", "credit", "account", "statement",
            "bmo", "wise", "chequing",
        ]
        matches = sum(1 for ind in indicators if ind in text_lower)
        return matches >= 2
    except Exception:
        return False


def detect_account_type(text: str) -> str:
    """Detect the account type (CAD, USD, or CreditCard) from statement text."""
    text_lower = text.lower()
    # BMO Credit Card indicators
    if "cashback" in text_lower and "mastercard" in text_lower:
        return "CreditCard"
    if "credit card" in text_lower and "posting date" in text_lower:
        return "CreditCard"
    # BMO USD indicators
    if "us$" in text_lower or "usd" in text_lower or "u.s. dollar" in text_lower:
        if "accounttype:usd" in text_lower.replace(" ", "") or "us$business" in text_lower:
            return "USD"
    # Default to CAD
    return "CAD"


def pdf_to_csv(file_path: Path) -> tuple[Path, str] | None:
    """Convert a bank statement PDF to a CSV file.

    Attempts to extract transaction tables from the PDF and write them
    as a CSV in a format the bank_parser can understand.

    Returns (csv_path, account_type) tuple, or None if extraction failed.
    account_type is "CAD", "USD", "CreditCard", or "PayPal".
    """
    text = extract_text_from_pdf(file_path)

    # PayPal statement PDFs — must be checked before BMO detection
    if _detect_paypal_pdf(text):
        csv_path = _parse_paypal_pdf(file_path, text)
        if csv_path:
            return csv_path, "PayPal"
        return None

    account_type = detect_account_type(text)

    # Credit Card PDFs have a different format — use dedicated parser
    if account_type == "CreditCard":
        csv_path = _parse_bmo_credit_card_pdf(text)
        if csv_path:
            return csv_path, account_type
        return None

    tables = extract_tables_from_pdf(file_path)

    # Try table-based extraction first
    if tables:
        csv_path = _tables_to_csv(tables, text)
        if csv_path:
            return csv_path, account_type

    # Fall back to text-based line parsing
    csv_path = _text_to_csv(text)
    if csv_path:
        return csv_path, account_type
    return None


def _detect_paypal_pdf(text: str) -> bool:
    """Check if PDF text looks like a PayPal statement."""
    text_lower = text.lower()
    return "paypal" in text_lower and (
        "transaction history" in text_lower
        or "activity statement" in text_lower
        or "merchant account id" in text_lower
    )


def _detect_paypal_date_order(text: str) -> str:
    """Detect date format in PayPal statement text.

    Returns 'dmy' for DD/MM/YYYY or 'mdy' for MM/DD/YYYY.
    Scans all slash-separated dates to find one that is unambiguous.
    """
    for m in re.finditer(r"(\d{1,2})/(\d{1,2})/(\d{4})", text):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            return "dmy"
        if b > 12:
            return "mdy"
    return "dmy"  # PayPal international default


def _parse_pp_date(date_str: str, order: str) -> str | None:
    """Parse a PayPal PDF date (e.g. '03/12/2000') into YYYY-MM-DD."""
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        if order == "dmy":
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return None


def _parse_paypal_pdf(file_path: str | Path, full_text: str) -> Path | None:
    """Parse a PayPal statement PDF into a PayPal-format CSV.

    Extracts transaction tables from all pages, determines currency from
    section headers ('Transaction History - CAD' / 'Transaction History - USD'),
    and writes a CSV that parse_paypal_csv can consume.
    """
    date_order = _detect_paypal_date_order(full_text)
    rows: list[dict[str, str]] = []

    with pdfplumber.open(str(file_path)) as pdf:
        current_currency = "CAD"

        for page in pdf.pages:
            page_text = page.extract_text() or ""

            # Collect currency sections in order on this page.
            # A page can have both CAD and USD sections — each transaction
            # table inherits the currency of the section header above it.
            page_sections: list[str] = []
            for line in page_text.split("\n"):
                if "Transaction History - USD" in line:
                    page_sections.append("USD")
                elif "Transaction History - CAD" in line:
                    page_sections.append("CAD")

            # If this page has section headers, start with the first one
            section_idx = 0
            if page_sections:
                current_currency = page_sections[0]

            tables = page.extract_tables()
            txn_table_count = 0
            for table in tables:
                if not table or len(table) < 2:
                    continue

                header = [str(c or "").strip().lower() for c in table[0]]

                # Only process PayPal transaction tables (Date + Gross columns)
                has_date = any("date" in h for h in header)
                has_gross = any("gross" in h for h in header)
                if not (has_date and has_gross):
                    continue

                # Map column indices
                col: dict[str, int | None] = {}
                for i, h in enumerate(header):
                    if "date" in h and "date" not in col:
                        col["date"] = i
                    elif "description" in h:
                        col["desc"] = i
                    elif "name" in h or "email" in h:
                        col["name"] = i
                    elif "gross" in h:
                        col["gross"] = i
                    elif "fee" in h:
                        col["fee"] = i
                    elif "net" in h:
                        col["net"] = i

                date_idx = col.get("date")
                if date_idx is None:
                    continue

                for row in table[1:]:
                    if not row or len(row) <= date_idx:
                        continue

                    raw_date = str(row[date_idx] or "").strip()
                    if not raw_date:
                        continue

                    parsed_date = _parse_pp_date(raw_date, date_order)
                    if not parsed_date:
                        continue

                    desc = str(row[col.get("desc", 1)] or "").strip() if col.get("desc") is not None and col["desc"] < len(row) else ""
                    name_raw = str(row[col.get("name", 2)] or "").strip() if col.get("name") is not None and col["name"] < len(row) else ""
                    gross = str(row[col.get("gross", 3)] or "").strip() if col.get("gross") is not None and col["gross"] < len(row) else "0"
                    fee = str(row[col.get("fee", 4)] or "").strip() if col.get("fee") is not None and col["fee"] < len(row) else "0"

                    # Name is the first line of the name/email cell
                    name = name_raw.split("\n")[0].strip()

                    # Extract transaction ID from description
                    txn_id = ""
                    id_match = re.search(r"ID:\s*(\S+)", desc)
                    if id_match:
                        txn_id = id_match.group(1)

                    # Type is the first line of description
                    txn_type = desc.split("\n")[0].strip()

                    rows.append({
                        "Date": parsed_date,
                        "Status": "Completed",
                        "Type": txn_type,
                        "Name": name,
                        "Gross": gross,
                        "Fee": fee,
                        "Currency": current_currency,
                        "Transaction ID": txn_id,
                    })

                # After processing a transaction table, advance to the next
                # currency section on this page (if one exists)
                txn_table_count += 1
                if page_sections and section_idx + 1 < len(page_sections):
                    section_idx += 1
                    current_currency = page_sections[section_idx]

    if not rows:
        logger.warning("No PayPal transactions extracted from PDF: %s", file_path)
        return None

    logger.info("Extracted %d PayPal transactions from PDF: %s", len(rows), file_path)
    return _write_paypal_pdf_csv(rows)


def _write_paypal_pdf_csv(rows: list[dict[str, str]]) -> Path:
    """Write PayPal transaction rows as a CSV that parse_paypal_csv can read."""
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".csv", mode="w", newline="", encoding="utf-8-sig",
    )
    fieldnames = ["Date", "Time", "TimeZone", "Name", "Type", "Status",
                  "Currency", "Gross", "Fee", "Net", "Transaction ID", "Subject"]
    writer = csv.DictWriter(tmp, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        writer.writerow({
            "Date": row["Date"],
            "Time": "",
            "TimeZone": "",
            "Name": row.get("Name", ""),
            "Type": row.get("Type", ""),
            "Status": row.get("Status", "Completed"),
            "Currency": row.get("Currency", "CAD"),
            "Gross": row.get("Gross", "0"),
            "Fee": row.get("Fee", "0"),
            "Net": "",
            "Transaction ID": row.get("Transaction ID", ""),
            "Subject": "",
        })

    tmp.close()
    return Path(tmp.name)


def _parse_bmo_credit_card_pdf(text: str) -> Path | None:
    """Parse BMO Credit Card PDF statement text.

    CC PDFs have lines like:
        Jan. 14 Jan. 15 CEDARVIEW SUPPLIES ON 41.25
        Jan. 16 Jan. 17 TRSF FROM/DE ACCT/CPT ACCOUNT-NUMBER 1,500.00 CR

    Format: TRANS_DATE POSTING_DATE DESCRIPTION AMOUNT [CR]
    Dates use abbreviated months: "Jan. 1", "Dec. 31"
    Negative amounts (credits/payments) are marked with "CR" suffix.
    Multi-line descriptions are joined.
    """
    rows = []

    # Extract statement year from "Statement date Jan. 28, 2000"
    # or "Statement period Dec. 29, 1999 - Jan. 28, 2000"
    year_match = re.search(
        r"statement\s+(?:date|period)\s+.*?(\d{4})",
        text, re.IGNORECASE,
    )
    statement_year = int(year_match.group(1)) if year_match else datetime.now().year

    # Also extract the period start year for cross-year statements
    period_match = re.search(
        r"statement\s+period\s+\w+\.?\s+\d+,?\s+(\d{4})\s*-",
        text, re.IGNORECASE,
    )
    period_start_year = int(period_match.group(1)) if period_match else statement_year

    # Pattern: "Mon. DD Mon. DD description amount [CR]"
    # The posting date (the second date) is the one recorded.
    # Descriptions that wrap to the next line are joined below.
    # A two-digit trans day fills the column, so pdfplumber emits the two dates
    # with no space between them ("Jan. 13Jan. 13"). \s* — not \s+ — after the
    # trans day keeps those rows matchable.
    txn_pattern = re.compile(
        r"^([A-Z][a-z]{2,8})\.?\s+(\d{1,2})\s*"  # trans date: "Dec. 31" or "Jan 1"
        r"([A-Z][a-z]{2,8})\.?\s+(\d{1,2})\s+"    # posting date: "Jan. 1"
        r"(.+?)\s+"                                  # description
        r"([\d,]+\.\d{2})"                           # amount
        r"(?:\s+(CR))?\s*$",                          # optional CR marker
        re.MULTILINE,
    )

    # Skip subtotal/total lines
    skip_phrases = ["subtotal", "total for card"]

    matches = txn_pattern.findall(text)

    for match in matches:
        _trans_mon, _trans_day, post_mon, post_day, description, amount_str, cr_marker = match

        desc_lower = description.lower()
        if any(skip in desc_lower for skip in skip_phrases):
            continue

        # Parse posting date
        try:
            post_month_num = datetime.strptime(post_mon[:3], "%b").month
        except ValueError:
            continue

        # Determine year: if posting month is Dec and statement year's month is Jan,
        # use previous year. Otherwise use statement year.
        if post_month_num == 12 and statement_year != period_start_year:
            year = period_start_year
        else:
            year = statement_year

        date_str = f"{year}{post_month_num:02d}{int(post_day):02d}"

        amount = Decimal(_clean_amount(amount_str))
        if cr_marker:
            # CR = credit (payment to card) — negative amount in CC CSV format
            txn_type = "CREDIT"
        else:
            txn_type = "DEBIT"

        rows.append((date_str, txn_type, amount, description.strip()))

    if not rows:
        return None

    return _write_cc_csv(rows)


def _write_cc_csv(rows: list[tuple[str, str, Decimal, str]]) -> Path:
    """Write credit card transaction rows as a BMO Credit Card CSV."""
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".csv", mode="w", newline="", encoding="utf-8",
    )
    writer = csv.writer(tmp)
    # BMO CC CSV format: Transaction Date, Posting Date, Description, Amount, Card Number
    writer.writerow(["Transaction Date", "Posting Date", "Description", "Transaction Amount", "Card Number"])

    for date_str, txn_type, amount, description in rows:
        # Format posting date as MM/DD/YYYY for parse_bmo_credit_card_csv
        year = date_str[:4]
        month = date_str[4:6]
        day = date_str[6:8]
        posting_date = f"{month}/{day}/{year}"

        # CC CSV: positive = charges, negative = payments/credits
        signed_amount = -amount if txn_type == "CREDIT" else amount
        writer.writerow([posting_date, posting_date, description, str(signed_amount), "0000000000000000"])

    tmp.close()
    return Path(tmp.name)


def _tables_to_csv(tables: list[list[list[str]]], full_text: str) -> Path | None:
    """Convert extracted PDF tables to a BMO-format CSV."""
    rows = []

    for table in tables:
        if not table or len(table) < 2:
            continue

        header = [str(cell or "").strip().lower() for cell in table[0]]

        # Detect BMO-style table (date, description, debit, credit, balance)
        has_date = any("date" in h for h in header)
        has_debit_credit = any("debit" in h or "withdrawal" in h for h in header) and \
                           any("credit" in h or "deposit" in h for h in header)
        has_amount = any("amount" in h for h in header)

        if not (has_date and (has_debit_credit or has_amount)):
            continue

        # Find column indices
        date_col = next((i for i, h in enumerate(header) if "date" in h), None)
        desc_col = next((i for i, h in enumerate(header) if "description" in h or "details" in h or "transaction" in h), None)
        debit_col = next((i for i, h in enumerate(header) if "debit" in h or "withdrawal" in h), None)
        credit_col = next((i for i, h in enumerate(header) if "credit" in h or "deposit" in h), None)
        amount_col = next((i for i, h in enumerate(header) if "amount" in h), None)

        if date_col is None:
            continue
        if desc_col is None:
            # Use the column right after date
            desc_col = date_col + 1 if date_col + 1 < len(header) else None

        for row in table[1:]:
            if not row or len(row) <= date_col:
                continue

            date_str = str(row[date_col] or "").strip()
            if not date_str:
                continue

            # Try to parse the date
            parsed_date = _parse_date(date_str)
            if not parsed_date:
                continue

            description = str(row[desc_col] or "").strip() if desc_col is not None and desc_col < len(row) else ""

            # Determine amount and transaction type
            if amount_col is not None and amount_col < len(row):
                amount_str = _clean_amount(str(row[amount_col] or ""))
                if amount_str:
                    amount = Decimal(amount_str)
                    txn_type = "DEBIT" if amount < 0 else "CREDIT"
                    rows.append((parsed_date, txn_type, abs(amount), description))
            elif debit_col is not None and credit_col is not None:
                debit_str = _clean_amount(str(row[debit_col] or "")) if debit_col < len(row) else ""
                credit_str = _clean_amount(str(row[credit_col] or "")) if credit_col < len(row) else ""

                if debit_str:
                    rows.append((parsed_date, "DEBIT", Decimal(debit_str), description))
                elif credit_str:
                    rows.append((parsed_date, "CREDIT", Decimal(credit_str), description))

    if not rows:
        return None

    return _write_bmo_csv(rows)


def _text_to_csv(text: str) -> Path | None:
    """Parse bank statement text line-by-line to extract transactions.

    Handles both layouts the BMO export format uses:
    1. Bank statement: "Jan02 Description 1,200.00 9,800.00"
       (MonDD Description debit_or_credit balance)
    2. Generic: "2000-01-15 DESCRIPTION -$40.00"
    """
    # Try BMO Business Banking format first
    rows = _parse_bmo_business_pdf(text)
    if rows:
        return _write_bmo_csv(rows)

    # Try generic patterns as fallback
    rows = _parse_generic_statement_text(text)
    if rows:
        return _write_bmo_csv(rows)

    return None


def _parse_bmo_business_pdf(text: str) -> list[tuple[str, str, Decimal, str]]:
    """Parse BMO Business Banking PDF statement text.

    The BMO export format prints transaction lines like:
        Jan02 IncomingWirePayment,INCOMINGWIRE 1,200.00 9,800.00
        Jan04 US$Transfer,USDTFR-ACCOUNT-NUMBER,AT1.3500 1,000.00 10,800.00
        Jan27 InterestEarned 20.00 10,820.00

    Each transaction line starts with MonDD (e.g., "Jan02", "Jan31").
    The last number is always the running balance.
    The middle number(s) are debit or credit amounts.
    """
    rows = []

    # Extract the statement end date from "For the period ending Month DD, YYYY"
    year_match = re.search(r"(?:period ending|statement)\s+.*?(\d{4})", text, re.IGNORECASE)
    statement_year = int(year_match.group(1)) if year_match else datetime.now().year

    # Extract the statement ending month to handle cross-year statements
    end_month_match = re.search(
        r"period ending\s+(\w+)\s+\d+,?\s+(\d{4})", text, re.IGNORECASE
    )
    if end_month_match:
        try:
            statement_end_month = datetime.strptime(end_month_match.group(1)[:3], "%b").month
        except ValueError:
            statement_end_month = 12
    else:
        statement_end_month = 12

    # Identify the BMO export format by the strings it prints in its header.
    if not re.search(r"(?:bmo|business banking)", text, re.IGNORECASE):
        if not re.search(r"(?:opening\s*balance|closing\s*totals)", text, re.IGNORECASE):
            return []

    # Determine which section has debits vs credits by finding the header line
    # "Amountsdebited Amountscredited" or similar
    _has_debit_credit_columns = bool(
        re.search(r"amounts?\s*debited.*amounts?\s*credited", text, re.IGNORECASE)
        or re.search(r"debit.*credit.*balance", text, re.IGNORECASE)
    )

    # Pattern: MonDD followed by description, then 1-3 amounts at end of line
    # e.g., "Feb02 IncomingWirePayment,INCOMINGWIRE 1,234.56 9,876.54"
    # The amounts at the end are: [debit] [credit] balance  OR  [amount] balance
    txn_pattern = re.compile(
        r"^([A-Z][a-z]{2})(\d{2})\s+"             # MonDD (e.g., Feb02)
        r"(.+?)\s+"                                 # description (greedy but stops before amounts)
        r"(-?[\d,]+\.\d{2})"                        # first amount (may be negative for overdraft)
        r"(?:\s+(-?[\d,]+\.\d{2}))?"                # optional second amount (may be negative balance)
        r"(?:\s+(-?[\d,]+\.\d{2}))?\s*$",           # optional third amount (balance)
        re.MULTILINE
    )

    # Skip these non-transaction lines that match the date pattern
    skip_phrases = [
        "openingbalance", "opening balance",
        "closingtotals", "closing totals",
        "closingbalance", "closing balance",
        "numberofitems", "number of items",
    ]

    matches = txn_pattern.findall(text)

    # Track running balance for debit/credit detection
    prev_balance = None

    for match in matches:
        month_str, day_str, description, amt1, amt2, amt3 = match
        desc_lower = description.lower().replace(" ", "")

        # Capture opening balance, then skip
        if "openingbalance" in desc_lower or "opening balance" in desc_lower.replace(" ", ""):
            amounts = [_clean_amount(a) for a in [amt1, amt2, amt3] if a]
            amounts = [a for a in amounts if a]
            if amounts:
                prev_balance = Decimal(amounts[-1])  # last amount is the balance
            continue

        # Skip other non-transaction lines
        if any(skip in desc_lower for skip in skip_phrases):
            continue

        # Parse date
        try:
            month_num = datetime.strptime(month_str, "%b").month
        except ValueError:
            continue

        # Handle cross-year statements: if the transaction month is after
        # the statement ending month, it belongs to the previous year.
        # E.g. a statement ending in January carrying December rows.
        txn_year = statement_year
        if month_num > statement_end_month:
            txn_year = statement_year - 1

        date_str = f"{txn_year}{month_num:02d}{day_str}"

        # Parse amounts — the rightmost amount is always the running balance
        amounts = [_clean_amount(a) for a in [amt1, amt2, amt3] if a]
        amounts = [a for a in amounts if a]

        if len(amounts) < 2:
            continue

        if len(amounts) == 3:
            # Three amounts: debit, credit, balance (separate columns)
            debit_amt = _clean_amount(amt1)
            credit_amt = _clean_amount(amt2)
            new_balance = Decimal(_clean_amount(amt3))
            if debit_amt and credit_amt:
                txn_type = "DEBIT"
                txn_amount = Decimal(debit_amt)
            elif debit_amt:
                txn_type = "DEBIT"
                txn_amount = Decimal(debit_amt)
            else:
                txn_type = "CREDIT"
                txn_amount = Decimal(credit_amt)
        else:
            # Two amounts: transaction amount + new running balance.
            # Determine debit vs credit by comparing balance change.
            txn_amount = abs(Decimal(amounts[0]))
            new_balance = Decimal(amounts[1])

            if prev_balance is not None:
                balance_change = new_balance - prev_balance
                if balance_change > 0:
                    txn_type = "CREDIT"
                else:
                    txn_type = "DEBIT"
            else:
                # No previous balance — fall back to description hints
                desc_upper = description.upper()
                credit_hints = ["INCOMING", "DEPOSIT", "INTEREST", "CREDIT", "REFUND"]
                if any(hint in desc_upper for hint in credit_hints):
                    txn_type = "CREDIT"
                else:
                    txn_type = "DEBIT"

        prev_balance = new_balance

        # Clean up description (remove run-together words from PDF extraction)
        clean_desc = description.strip().rstrip(",")

        rows.append((date_str, txn_type, txn_amount, clean_desc))

    return rows


def _parse_generic_statement_text(text: str) -> list[tuple[str, str, Decimal, str]]:
    """Parse generic bank statement text with standard date formats."""
    rows = []

    patterns = [
        # YYYY-MM-DD + description + amount
        re.compile(
            r"(\d{4}[-/]\d{2}[-/]\d{2})\s+"
            r"(.+?)\s+"
            r"(-?\$?[\d,]+\.\d{2})\s*$",
            re.MULTILINE
        ),
        # Mon DD, YYYY + description + amount
        re.compile(
            r"([A-Z][a-z]{2}\s+\d{1,2},?\s+\d{4})\s+"
            r"(.+?)\s+"
            r"(-?\$?[\d,]+\.\d{2})\s*$",
            re.MULTILINE
        ),
        # MM/DD/YYYY + description + amount
        re.compile(
            r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+"
            r"(.+?)\s+"
            r"(-?\$?[\d,]+\.\d{2})\s*$",
            re.MULTILINE
        ),
    ]

    for pattern in patterns:
        matches = pattern.findall(text)
        if len(matches) >= 2:
            for date_str, description, amount_str in matches:
                parsed_date = _parse_date(date_str)
                if not parsed_date:
                    continue
                clean_amount = _clean_amount(amount_str)
                if not clean_amount:
                    continue
                amount = Decimal(clean_amount)
                txn_type = "DEBIT" if amount < 0 else "CREDIT"
                rows.append((parsed_date, txn_type, abs(amount), description.strip()))
            break

    return rows


def _write_bmo_csv(rows: list[tuple[str, str, Decimal, str]]) -> Path:
    """Write transaction rows as a BMO-format CSV."""
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".csv", mode="w", newline="", encoding="utf-8"
    )
    writer = csv.writer(tmp)
    writer.writerow(["Account Number", "Transaction Type", "Date Posted", "Transaction Amount", "Description"])

    for date_str, txn_type, amount, description in rows:
        # Format amount with sign
        signed_amount = -amount if txn_type == "DEBIT" else amount
        writer.writerow(["0000000", txn_type, date_str, str(signed_amount), description])

    tmp.close()
    return Path(tmp.name)


def _parse_date(date_str: str) -> str | None:
    """Try to parse a date string into YYYYMMDD format."""
    date_str = date_str.strip()

    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%m/%d",
        "%b %d, %Y",
        "%b %d %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%d-%b-%Y",
        "%d %b %Y",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            # If year is missing (e.g., MM/DD), assume current year
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y%m%d")
        except ValueError:
            continue

    return None


def _clean_amount(amount_str: str) -> str:
    """Clean an amount string: remove $, commas, whitespace."""
    if not amount_str:
        return ""
    cleaned = amount_str.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned or cleaned == "-":
        return ""
    try:
        Decimal(cleaned)
        return cleaned
    except InvalidOperation:
        return ""
