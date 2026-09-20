"""PayPal API integration — pulls transaction history automatically.

Uses the PayPal REST API with OAuth2 client credentials to fetch transactions.
Read-only — only fetches transaction history, never sends money.

Setup — credentials held per user in the credential store:
1. Log into PayPal Developer Dashboard → Apps & Credentials
2. Create a REST API app (Live mode, Merchant type)
3. Enable "Transaction Search" permission
4. Copy Client ID + Secret into Settings → Connections

Setup — credentials read from the environment instead:
1. Add to .env:
   PAYPAL_CLIENT_ID=your_client_id_here
   PAYPAL_CLIENT_SECRET=your_client_secret_here
   PAYPAL_SANDBOX=false
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from core.sync_utils import get_http_client, last_day_of_month, month_range, stable_hash
from models.transaction import AccountType, Transaction

logger = logging.getLogger(__name__)

PAYPAL_LIVE_URL = "https://api-m.paypal.com"
PAYPAL_SANDBOX_URL = "https://api-m.sandbox.paypal.com"


# Module-local names for the shared sync helpers
_stable_hash = stable_hash
_month_range = month_range
_last_day_of_month = last_day_of_month


# ── HTTP Client ─────────────────────────────────────────────────────


def _get_base_url() -> str:
    """Return the PayPal API base URL based on sandbox setting."""
    sandbox = os.getenv("PAYPAL_SANDBOX", "false").lower() in ("true", "1", "yes")
    return PAYPAL_SANDBOX_URL if sandbox else PAYPAL_LIVE_URL


def _get_client() -> httpx.Client:
    """Create an httpx client with proper SSL."""
    return get_http_client(timeout=30)


# ── Credential Helpers ──────────────────────────────────────────────


def get_paypal_credentials() -> tuple[str, str] | None:
    """Get PayPal client ID and secret from the environment.

    Returns (client_id, client_secret) or None if not configured.
    """
    client_id = os.getenv("PAYPAL_CLIENT_ID", "")
    client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
    if (
        client_id
        and client_id != "your_paypal_client_id_here"
        and client_secret
        and client_secret != "your_paypal_client_secret_here"
    ):
        return client_id, client_secret
    return None


# ── Token Management ────────────────────────────────────────────────

# In-process token cache keyed by user_id → (token, expiry_timestamp)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TTL_SECONDS = 9 * 3600  # 9 hours (PayPal access token lifetime)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True)
def get_access_token(client_id: str, client_secret: str) -> str | None:
    """Obtain an OAuth2 access token using client credentials grant.

    Returns the access token string, or None on failure.
    """
    base_url = _get_base_url()
    client = _get_client()
    try:
        resp = client.post(
            f"{base_url}/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("access_token")
    except Exception as e:
        logger.error("Failed to get PayPal access token: %s", e)
        return None
    finally:
        client.close()


def get_access_token_cached(
    user_id: str, client_id: str, client_secret: str
) -> str | None:
    """Return a cached access token, or fetch a new one if expired/missing."""
    now = time.time()
    cached = _TOKEN_CACHE.get(user_id)
    if cached:
        token, expiry = cached
        if now < expiry:
            return token
    token = get_access_token(client_id, client_secret)
    if token:
        _TOKEN_CACHE[user_id] = (token, now + _TTL_SECONDS)
    return token


def test_credentials(client_id: str, client_secret: str) -> bool:
    """Test PayPal credentials by attempting to get an access token.

    Returns True if credentials are valid, False otherwise.
    """
    token = get_access_token(client_id, client_secret)
    return token is not None


# ── T-Code Filtering ────────────────────────────────────────────────

# Denylist prefixes (checked first — takes priority)
_DENY_PREFIXES = ("T02", "T20", "T21", "T13", "T98")
# Allowlist prefixes
_ALLOW_PREFIXES = ("T00", "T01", "T11", "T03", "T04")


def _should_import_transaction(txn_info: dict) -> bool:
    """Determine if a PayPal transaction should be imported based on T-code and status.

    Rules:
    1. Status must be 'S' (Successful). Skip P (Pending), V (Reversed), D (Denied).
    2. Denylist: T02xx (conversions), T20xx (intra-account), T21xx (holds),
       T13xx (authorizations), T98xx (display-only).
    3. Allowlist: T00xx (payments), T01xx (fees), T11xx (refunds),
       T03xx-T04xx (bank transfers), T1201 (chargebacks).
    4. Default: skip unknown codes.
    """
    status = txn_info.get("transaction_status", "")
    if status != "S":
        return False

    event_code = txn_info.get("transaction_event_code", "")
    if not event_code:
        return True  # No code — import conservatively

    for prefix in _DENY_PREFIXES:
        if event_code.startswith(prefix):
            return False

    for prefix in _ALLOW_PREFIXES:
        if event_code.startswith(prefix):
            return True

    if event_code == "T1201":
        return True

    return False


# ── Counterparty Extraction ─────────────────────────────────────────


def _extract_counterparty(txn_data: dict) -> str:
    """Extract the best counterparty name from PayPal transaction data.

    Priority chain:
    1. payer_info.payer_name.alternate_full_name (business name)
    2. payer_info.payer_name.given_name + surname
    3. transaction_info.transaction_subject
    4. cart_info.item_details[0].item_name
    5. payer_info.email_address domain
    6. "PayPal" fallback
    """
    # Try payer_info first
    payer_info = txn_data.get("payer_info", {})
    payer_name = payer_info.get("payer_name", {})

    # 1. Business name (alternate_full_name)
    alt_name = payer_name.get("alternate_full_name", "").strip()
    if alt_name:
        return alt_name

    # 2. Given name + surname
    given = payer_name.get("given_name", "").strip()
    surname = payer_name.get("surname", "").strip()
    if given or surname:
        return f"{given} {surname}".strip()

    txn_info = txn_data.get("transaction_info", {})

    # 3. Transaction subject
    subject = txn_info.get("transaction_subject", "").strip()
    if subject:
        return subject

    # 4. Cart item name
    cart_info = txn_data.get("cart_info", {})
    items = cart_info.get("item_details", [])
    if items and isinstance(items, list) and len(items) > 0:
        item_name = items[0].get("item_name", "").strip()
        if item_name:
            return item_name

    # 5. Email domain
    email = payer_info.get("email_address", "").strip()
    if email and "@" in email:
        domain = email.split("@")[1].split(".")[0]
        return domain.capitalize()

    # 6. Fallback
    return "PayPal"


# ── Transaction Fetching ────────────────────────────────────────────


def _parse_paypal_date(date_str: str) -> date:
    """Parse a PayPal ISO 8601 date string to a Python date object."""
    if not date_str:
        return date.today()
    try:
        # PayPal returns dates like "2000-01-15T10:30:00+0000"
        cleaned = date_str.replace("+0000", "+00:00").replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        return date.today()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True)
def fetch_paypal_transactions_for_month(
    access_token: str,
    year: int,
    month: int,
    currency: str,
    today: date,
) -> list[dict]:
    """Fetch PayPal transactions for a single month and currency.

    Returns raw transaction detail dicts from the PayPal API.
    Handles pagination (page_size=100, 1-indexed pages).
    """
    start_date = date(year, month, 1)
    end_date = min(_last_day_of_month(year, month), today)

    # PayPal requires ISO 8601 datetime with timezone
    start_dt = f"{start_date}T00:00:00-0000"
    end_dt = f"{end_date}T23:59:59-0000"

    base_url = _get_base_url()
    client = _get_client()
    all_txns: list[dict] = []

    try:
        page = 1
        total_pages = 1

        while page <= total_pages:
            resp = client.get(
                f"{base_url}/v1/reporting/transactions",
                params={
                    "start_date": start_dt,
                    "end_date": end_dt,
                    "fields": "all",
                    "page_size": 100,
                    "page": page,
                    "transaction_currency": currency,
                    "balance_affecting_records_only": "Y",
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            total_pages = data.get("total_pages", 1)
            if total_pages == 0:
                break

            for txn_detail in data.get("transaction_details", []):
                all_txns.append(txn_detail)

            page += 1
            if page <= total_pages:
                time.sleep(0.1)  # Courtesy delay between pages

    except Exception:
        raise  # Let tenacity handle retries
    finally:
        client.close()

    return all_txns


def _txn_detail_to_transaction(
    txn_data: dict,
    currency: str,
    source_file: str,
) -> Transaction | None:
    """Convert a PayPal API transaction detail to a Transaction model object.

    Returns None if the transaction should be skipped (T-code filtering).
    """
    txn_info = txn_data.get("transaction_info", {})

    # T-code + status filtering
    if not _should_import_transaction(txn_info):
        return None

    # Extract fields
    txn_id = txn_info.get("transaction_id", "")
    event_code = txn_info.get("transaction_event_code", "")

    # Amount
    amount_obj = txn_info.get("transaction_amount", {})
    amount_str = amount_obj.get("value", "0")
    amount_val = Decimal(amount_str)
    txn_type = "CREDIT" if amount_val >= 0 else "DEBIT"

    # Date
    date_str = txn_info.get("transaction_initiation_date", "")
    txn_date = _parse_paypal_date(date_str)

    # Counterparty / description
    counterparty = _extract_counterparty(txn_data)

    # Subject or note for richer description
    subject = txn_info.get("transaction_subject", "").strip()
    note = txn_info.get("transaction_note", "").strip()
    description = subject or note or counterparty

    # Composite dedup key: transaction_id + event_code
    # PayPal's transaction_id alone is NOT unique (same ID can appear for
    # balance-affecting and non-balance-affecting rows)
    dedup_key = f"{txn_id}:{event_code}" if txn_id else f"unknown:{event_code}:{amount_str}:{date_str}"
    source_row = _stable_hash(dedup_key)

    return Transaction(
        account=AccountType.PAYPAL,
        transaction_type=txn_type,
        date_posted=txn_date,
        amount=amount_val,
        currency=currency,
        description=description,
        source_file=source_file,
        source_row=source_row,
    )


# ── Monthly Sync Orchestrator ───────────────────────────────────────


def sync_paypal_by_month(
    client_id: str,
    client_secret: str,
    db,  # DatabasePg instance
    user_id: str,
    progress_callback: Callable[[str], None] | None = None,
    since_date: date | None = None,
    until_date: date | None = None,
    currencies: list[str] | None = None,
) -> dict[str, dict]:
    """Sync PayPal transactions month-by-month, mirroring sync_wise_by_month.

    For each currency and each month in the date range:
    - Past months already synced: SKIP (no API call)
    - Current month: DELETE existing + re-fetch
    - New months: fetch and insert

    Returns {source_file: {"new": N, "total": M}} per month-currency chunk.
    """

    def _log(msg: str) -> None:
        logger.info("[PAYPAL-SYNC] %s", msg)
        if progress_callback:
            progress_callback(msg)

    effective_currencies = currencies or ["USD", "CAD"]
    today = date.today()
    effective_until = until_date or today

    # Determine date range
    if since_date:
        start_month = (since_date.year, since_date.month)
    else:
        # Default: 12 months back
        fallback = today - timedelta(days=365)
        start_month = (fallback.year, fallback.month)

    end_month = (effective_until.year, effective_until.month)

    # Get access token
    token = get_access_token_cached(user_id, client_id, client_secret)
    if not token:
        _log("ERROR: Failed to obtain PayPal access token")
        return {"paypal": {"error": "PayPal authentication failed — check your Client ID and Secret"}}

    results: dict[str, dict] = {}

    for currency in effective_currencies:
        # Get existing source files for this currency
        prefix = f"paypal_{currency}_"
        try:
            existing_source_files = db.get_source_files_by_prefix(prefix)
        except Exception:
            existing_source_files = []

        synced_months: set[tuple[int, int]] = set()
        for sf in existing_source_files:
            # Parse "paypal_USD_202601" -> (2026, 1)
            key = sf if isinstance(sf, str) else sf.get("source_file", "")
            parts = key.replace(prefix, "")
            if len(parts) == 6 and parts.isdigit():
                synced_months.add((int(parts[:4]), int(parts[4:6])))

        months = _month_range(start_month, end_month)
        total_months = len(months)

        for idx, (year, month) in enumerate(months, 1):
            source_file = f"paypal_{currency}_{year:04d}{month:02d}"
            month_label = f"{currency} {date(year, month, 1).strftime('%b %Y')}"

            is_current_month = (year == today.year and month == today.month)
            is_past_synced = (year, month) in synced_months and not is_current_month

            if is_past_synced:
                _log(f"Syncing PayPal {month_label}... ({idx}/{total_months}) SKIP (past month)")
                continue

            if is_current_month and (year, month) in synced_months:
                _log(f"Syncing PayPal {month_label}... ({idx}/{total_months}) DELETE + re-fetch (current month)")
                try:
                    db.delete_transactions_by_source_file(source_file)
                except Exception as e:
                    logger.warning("Failed to delete existing PayPal txns for %s: %s", source_file, e)
            else:
                _log(f"Syncing PayPal {month_label}... ({idx}/{total_months})")

            try:
                raw_txns = fetch_paypal_transactions_for_month(
                    token, year, month, currency, today
                )
            except Exception as e:
                logger.error("PayPal fetch failed for %s: %s", source_file, e)
                results[source_file] = {"error": str(e)}
                continue

            new_count = 0
            total_count = 0

            for txn_data in raw_txns:
                txn = _txn_detail_to_transaction(txn_data, currency, source_file)
                if txn is None:
                    continue  # Filtered by T-code

                total_count += 1
                try:
                    db.insert_transaction(txn)
                    new_count += 1
                except Exception:
                    pass  # Duplicate — silently skip

            results[source_file] = {"new": new_count, "total": total_count}
            _log(f"  {month_label}: {new_count} new / {total_count} total transactions")

            # Courtesy delay between months
            time.sleep(0.1)

    return results


# ── Flat Range Fetch ────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True)
def fetch_paypal_transactions(
    access_token: str,
    since: date | None = None,
    until: date | None = None,
) -> list[Transaction]:
    """Fetch transactions from the PayPal Reporting API over one date range.

    Pulls the whole range in a single pass and returns Transaction objects
    without touching the database. ``sync_paypal_by_month()`` is the
    month-by-month path that skips months already stored.
    """
    if since is None:
        since = date.today() - timedelta(days=30)
    if until is None:
        until = date.today()

    base_url = _get_base_url()
    client = _get_client()
    transactions = []

    try:
        start_dt = f"{since}T00:00:00-0000"
        end_dt = f"{until}T23:59:59-0000"

        page = 1
        total_pages = 1

        while page <= total_pages:
            resp = client.get(
                f"{base_url}/v1/reporting/transactions",
                params={
                    "start_date": start_dt,
                    "end_date": end_dt,
                    "fields": "all",
                    "page_size": 100,
                    "page": page,
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            total_pages = data.get("total_pages", 1)
            if total_pages == 0:
                break

            for txn in data.get("transaction_details", []):
                txn_info = txn.get("transaction_info", {})

                if not _should_import_transaction(txn_info):
                    continue

                txn_id = txn_info.get("transaction_id", "")
                event_code = txn_info.get("transaction_event_code", "")

                amount_obj = txn_info.get("transaction_amount", {})
                amount_str = amount_obj.get("value", "0")
                amount_val = Decimal(amount_str)
                txn_currency = amount_obj.get("currency_code", "CAD")
                txn_type = "CREDIT" if amount_val >= 0 else "DEBIT"

                date_str = txn_info.get("transaction_initiation_date", "")
                txn_date = _parse_paypal_date(date_str)

                counterparty = _extract_counterparty(txn)
                subject = txn_info.get("transaction_subject", "").strip()
                note = txn_info.get("transaction_note", "").strip()
                description = subject or note or counterparty

                dedup_key = f"{txn_id}:{event_code}" if txn_id else f"unknown:{event_code}:{amount_str}:{date_str}"

                transactions.append(Transaction(
                    account=AccountType.PAYPAL,
                    transaction_type=txn_type,
                    date_posted=txn_date,
                    amount=amount_val,
                    currency=txn_currency,
                    description=description,
                    source_file="paypal_api",
                    source_row=_stable_hash(dedup_key),
                ))

            page += 1

    except Exception as e:
        logger.error("Failed to fetch PayPal transactions: %s", e)
    finally:
        client.close()

    return transactions
