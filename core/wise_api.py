"""Wise API integration — pulls transactions automatically.

Uses the Wise API with a read-only API token to fetch transactions.
No OAuth flow needed — user generates a token in Wise settings.

Setup:
1. Log into Wise → Settings → API tokens
2. Create a Read-only token
3. Add to .env: WISE_API_TOKEN=your_token_here
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from core.sync_utils import get_http_client, last_day_of_month, month_range, stable_hash
from models.transaction import AccountType, Transaction

logger = logging.getLogger(__name__)

WISE_API_PROD = "https://api.transferwise.com"
WISE_API_SANDBOX = "https://api.sandbox.transferwise.tech"


def _get_api_base() -> str:
    """Return Wise API base URL, using sandbox if configured."""
    if os.getenv("WISE_SANDBOX", "").lower() in ("true", "1", "yes"):
        return WISE_API_SANDBOX
    return WISE_API_PROD


WISE_API_BASE = _get_api_base()


def _get_client() -> httpx.Client:
    """Create an httpx client with proper SSL."""
    return get_http_client(timeout=30)


def get_wise_token() -> str | None:
    """Get the Wise API token from environment."""
    token = os.getenv("WISE_API_TOKEN", "")
    return token if token and token != "your_wise_api_token_here" else None


# Module-local names for the shared sync helpers
_stable_hash = stable_hash
_month_range = month_range
_last_day_of_month = last_day_of_month

# Wise's own marker for "this row is a fee Wise charged", from ``details.type``
# on a balance-statement row. Everything else Wise sends there ("TRANSFER",
# "CONVERSION", "DEPOSIT", "CARD", …) is ordinary account activity.
_WISE_FEE_DETAIL_TYPES = frozenset({"FEE", "FEES"})

# The prefix a fee row gets when Wise's own wording does not already read as a
# fee to the rule pre-pass. Chosen to match ``\bWISE\s+(?:\w+\s+)?FEE\b`` in
# core/categorization_agent/rule_prepass.py.
_WISE_FEE_PREFIX = "Wise fee: "


def _label_fee_description(detail_type: str | None, description: str) -> str:
    """Return ``description``, prefixed so a Wise FEE row reads as a fee.

    Wise bills its transfer fee as a separate statement row whose only clue in
    the free text is wording such as ``"Wise Charges for: TRANSFER-…"``. That
    wording belongs to Wise and can change, and text-only recognition breaks
    when it does: the row stops reading as a bank charge, falls through to the
    receipt matcher, and can take its category from whatever unrelated receipt
    it weakly matches.

    ``details.type == "FEE"`` is the structured field that says it outright, so
    that is the signal used here. Wise's own text is kept — it carries the
    TRANSFER reference the fee belongs to, and rewriting it would make every
    historical row look different on the next sync — and a prefix is added ONLY
    when the text does not already trip the "Interest and bank charges" rule.
    So wording that already reads as a fee is stored unchanged, and any other
    wording arrives as "Wise fee: <whatever Wise now says>".
    """
    if not description:
        return description
    if str(detail_type or "").strip().upper() not in _WISE_FEE_DETAIL_TYPES:
        return description
    from core.categorization_agent.rule_prepass import match_category
    if match_category(description) == "Interest and bank charges":
        return description
    return f"{_WISE_FEE_PREFIX}{description}"


def get_profile_id(token: str, preferred_profile_id: int | None = None) -> int | None:
    """Get the Wise profile ID (business preferred, personal as fallback).

    If preferred_profile_id is provided, verifies it exists and returns it.
    Otherwise auto-selects: single business > single personal > first available.
    Raises on auth/API errors so the caller can report them accurately.
    """
    token = token.strip()
    client = _get_client()
    try:
        resp = client.get(
            f"{WISE_API_BASE}/v2/profiles",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        profiles = resp.json()

        if not profiles:
            return None

        # If a preferred profile is set, verify it exists and return it
        if preferred_profile_id:
            for p in profiles:
                if p.get("id") == preferred_profile_id:
                    return preferred_profile_id
            logger.warning(
                "Preferred Wise profile %s not found among %d profiles",
                preferred_profile_id, len(profiles),
            )
            return None

        # Separate business and personal profiles
        business_profiles = [
            p for p in profiles
            if str(p.get("type", "")).upper() == "BUSINESS"
        ]
        personal_profiles = [
            p for p in profiles
            if str(p.get("type", "")).upper() == "PERSONAL"
        ]

        # Auto-select: single business > single personal > first profile
        if len(business_profiles) == 1:
            return business_profiles[0]["id"]
        if len(personal_profiles) == 1 and not business_profiles:
            return personal_profiles[0]["id"]
        if len(profiles) == 1:
            return profiles[0]["id"]

        # Multiple profiles — return first available rather than failing
        # (business preferred over personal)
        if business_profiles:
            return business_profiles[0]["id"]
        return profiles[0]["id"]
    except httpx.HTTPStatusError as e:
        logger.error("Wise API HTTP error: %s %s", e.response.status_code, e.response.text[:200] if e.response.text else "")
        if e.response.status_code in (401, 403):
            raise  # Auth errors won't fix themselves — skip retry
        raise  # Let caller handle other errors
    except Exception as e:
        logger.error("Failed to get Wise profile: %s", e)
        raise  # Let caller distinguish error types
    finally:
        client.close()


def list_wise_profiles(token: str) -> list[dict]:
    """Return all Wise profiles with balance summaries for user selection.

    Returns list of dicts with id, type, name, and balances for each profile.
    """
    client = _get_client()
    try:
        resp = client.get(
            f"{WISE_API_BASE}/v2/profiles",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        raw_profiles = resp.json()

        results = []
        for p in raw_profiles:
            pid = p.get("id")
            ptype = str(p.get("type", "")).upper()
            details = p.get("details") or {}
            # v2 has businessName/fullName/firstName/lastName at top level
            name = (
                p.get("fullName")
                or p.get("businessName")
                or details.get("businessName")
                or details.get("name")
                or ((p.get("firstName") or details.get("firstName") or "") + " " + (p.get("lastName") or details.get("lastName") or "")).strip()
                or f"Profile {pid}"
            )

            # Fetch balances for this profile
            profile_balances = []
            try:
                bals_resp = client.get(
                    f"{WISE_API_BASE}/v4/profiles/{pid}/balances?types=STANDARD",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if bals_resp.status_code == 200:
                    for b in bals_resp.json():
                        profile_balances.append({
                            "currency": b.get("currency"),
                            "amount": b.get("amount", {}).get("value", 0),
                        })
            except Exception:
                pass

            results.append({
                "id": pid,
                "type": ptype,
                "name": name,
                "balances": profile_balances,
            })

        return results
    except Exception as e:
        logger.error("Failed to list Wise profiles: %s", e)
        return []
    finally:
        client.close()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True)
def get_borderless_accounts(token: str, profile_id: int) -> list[dict]:
    """Get Wise multi-currency account balances."""
    client = _get_client()
    try:
        resp = client.get(
            f"{WISE_API_BASE}/v4/profiles/{profile_id}/balances?types=STANDARD",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to get Wise balances: %s", e)
        return []
    finally:
        client.close()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True)
def fetch_wise_transactions(
    token: str,
    profile_id: int,
    currency: str = "USD",
    since: date | None = None,
    until: date | None = None,
    account_id: int | None = None,
) -> list[Transaction]:
    """Fetch transactions from Wise API for a specific currency.

    Args:
        token: Wise API bearer token.
        profile_id: Wise profile ID.
        currency: Currency code (USD, CAD, etc.).
        since: Start date (default: 30 days ago).
        until: End date (default: today).
        account_id: Balance account ID. If provided, skips the balances lookup
                    (avoids redundant API calls and rate limiting).

    Returns list of Transaction objects.
    """
    if since is None:
        since = date.today() - timedelta(days=30)
    if until is None:
        until = date.today()

    client = _get_client()
    transactions = []

    try:
        # Use provided account_id or look it up
        if account_id is None:
            balances = get_borderless_accounts(token, profile_id)
            for bal in balances:
                if bal.get("currency") == currency:
                    account_id = bal.get("id")
                    break

        if account_id is None:
            logger.warning("No Wise account found for currency %s", currency)
            return []

        # Fetch statement for the period
        resp = client.get(
            f"{WISE_API_BASE}/v1/profiles/{profile_id}/balance-statements/{account_id}/statement",
            params={
                "currency": currency,
                "intervalStart": f"{since}T00:00:00.000Z",
                "intervalEnd": f"{until}T23:59:59.999Z",
                "type": "FLAT",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        raw_txns = data.get("transactions", [])
        logger.info(
            "Wise %s %s→%s: API returned %d transactions (account_id=%s)",
            currency, since, until, len(raw_txns), account_id,
        )
        if not raw_txns:
            # Log response keys and balances for debugging empty results
            logger.info(
                "Wise %s EMPTY response keys: %s, startBalance=%s, endBalance=%s",
                currency,
                list(data.keys()),
                data.get("startOfStatementBalance"),
                data.get("endOfStatementBalance"),
            )

        for i, txn in enumerate(raw_txns, start=1):
            amount_obj = txn.get("amount", {})
            amount_val = Decimal(str(amount_obj.get("value", 0)))
            txn_currency = amount_obj.get("currency", currency)

            txn_type = "CREDIT" if amount_val >= 0 else "DEBIT"

            date_str = txn.get("date", "")
            if date_str:
                txn_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
            else:
                txn_date = date.today()

            details = txn.get("details") or {}
            ref = details.get("description", "")
            txn_ref = txn.get("referenceNumber", "")
            description = ref or txn_ref or f"Wise transaction {txn.get('type', '')}"
            description = _label_fee_description(details.get("type"), description)

            # Use referenceNumber as stable source_row for dedup.
            # Falls back to enumeration index if referenceNumber is missing.
            source_row = _stable_hash(txn_ref) if txn_ref else i

            transactions.append(Transaction(
                account=AccountType.WISE,
                transaction_type=txn_type,
                date_posted=txn_date,
                amount=amount_val,
                currency=txn_currency,
                description=description,
                source_file="wise_api",
                source_row=source_row,
            ))

    except Exception as e:
        logger.error("Failed to fetch Wise %s transactions: %s", currency, e, exc_info=True)
    finally:
        client.close()

    return transactions


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True)
def download_wise_statement(
    token: str,
    profile_id: int,
    account_id: int,
    start_date: date,
    end_date: date,
    output_dir: str | Path,
    currency: str = "USD",
) -> str | None:
    """Download a Wise statement as a PDF for a date range.

    Args:
        token: Wise API bearer token.
        profile_id: Wise profile ID.
        account_id: Balance account ID.
        start_date: Statement start date.
        end_date: Statement end date.
        output_dir: Directory to save the PDF.
        currency: Currency code for the statement.

    Returns:
        Path to the downloaded PDF, or None on failure.
    """
    api_base = _get_api_base()
    client = _get_client()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    try:
        resp = client.get(
            f"{api_base}/v1/profiles/{profile_id}/balance-statements/{account_id}/statement",
            params={
                "currency": currency,
                "intervalStart": f"{start_date}T00:00:00.000Z",
                "intervalEnd": f"{end_date}T23:59:59.999Z",
                "type": "FLAT",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/pdf",
            },
        )
        resp.raise_for_status()

        filename = f"wise_statement_{currency}_{start_date}_{end_date}.pdf"
        dest = output_path / filename
        dest.write_bytes(resp.content)
        logger.info("Downloaded Wise statement: %s", dest)
        return str(dest)
    except Exception as e:
        logger.error("Failed to download Wise statement: %s", e)
        return None
    finally:
        client.close()


def _store_wise_statement_pdf(
    *,
    token: str,
    profile_id: int,
    account_id: int,
    currency: str,
    start_date: date,
    end_date: date,
    source_file: str,
    db,
    user_id: str,
    log: Callable[[str], None],
) -> None:
    """Download a Wise balance-statement PDF and store it in the file store.

    Records the stored path against ``source_file`` in ``processed_files`` so
    the ``/api/transactions/statement-file`` endpoint can later serve it for
    viewing in the UI.
    """
    # Skip if already archived for this user + source_file.
    try:
        if db.get_processed_file_stored_path(source_file):
            return
    except Exception:
        pass

    api_base = _get_api_base()
    client = _get_client()
    try:
        resp = client.get(
            f"{api_base}/v1/profiles/{profile_id}/balance-statements/{account_id}/statement",
            params={
                "currency": currency,
                "intervalStart": f"{start_date}T00:00:00.000Z",
                "intervalEnd": f"{end_date}T23:59:59.999Z",
                "type": "FLAT",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/pdf",
            },
        )
        resp.raise_for_status()
        pdf_bytes = resp.content
    finally:
        client.close()

    if not pdf_bytes:
        log(f"{source_file}: empty statement PDF, skipping archive")
        return

    from core import file_storage

    # Use the source_file as the filename so the stored object is easy to find
    # and the Content-Disposition on download is human-readable.
    filename = f"{source_file}.pdf"
    stored_path = file_storage.upload_file(
        user_id=user_id,
        file_bytes=pdf_bytes,
        filename=filename,
        content_type="application/pdf",
    )

    import hashlib as _hashlib
    file_hash = _hashlib.sha256(pdf_bytes).hexdigest()
    try:
        db.insert_processed_file(file_hash, source_file, stored_path=stored_path)
    except Exception as e:
        # A processed_files row may already exist from a prior run without a
        # stored_path — update it instead.
        log(f"{source_file}: processed_files insert fallback ({e})")
        try:
            db.update_processed_file_stored_path(source_file, stored_path)
        except Exception as e2:
            log(f"{source_file}: could not record stored_path: {e2}")
            return

    log(f"{source_file}: archived statement PDF to {stored_path}")


def sync_wise_by_month(
    token: str,
    profile_id: int,
    db,  # DatabasePg instance
    user_id: str,
    progress_callback: Callable[[str], None] | None = None,
    since_date: date | None = None,
    until_date: date | None = None,
) -> dict[str, dict]:
    """Fetch Wise transactions month-by-month, one source_file per month-currency pair.

    For each currency account on Wise:
    1. Determine the account creation date (earliest possible data).
    2. Compute calendar month intervals from creation (or since_date) to today (or until_date).
    3. Skip past months already synced in the database.
    4. Delete + re-fetch the current month (may have new transactions).
    5. Insert transactions with source_file = "wise_{CURRENCY}_{YYYYMM}".

    Args:
        token: Wise API bearer token.
        profile_id: Wise profile ID.
        db: DatabasePg instance (user-scoped).
        user_id: User UUID string.
        progress_callback: Optional callable for progress messages.
        since_date: If provided, override the per-account creation-time start and begin
                    syncing from this date's month. Months before this date are skipped.
                    The effective start is max(since_date month, account creation month)
                    so no month before the account existed is ever requested.
        until_date: If provided, stop syncing after this date's month (inclusive).
                    Defaults to today.

    Returns:
        Dict mapping source_file keys to result dicts. Example:
        {
            "wise_USD_202601": {"new": 12, "total": 15},
            "wise_CAD_202601": {"error": "API timeout"},
        }
    """
    def _log(msg: str):
        logger.info("[WISE-SYNC] %s", msg)
        if progress_callback:
            progress_callback(msg)

    accounts = get_borderless_accounts(token, profile_id)
    today = date.today()
    effective_until = until_date if until_date else today
    end_month = (effective_until.year, effective_until.month)
    results: dict[str, dict] = {}

    _log(f"Got {len(accounts)} balance accounts from Wise API")
    if accounts:
        _log(f"Accounts: {len(accounts)} balance accounts")

    if not accounts:
        _log("WARNING: No Wise balance accounts found")
        return results

    for acct in accounts:
        currency = acct.get("currency")
        acct_id = acct.get("id")
        if not currency:
            _log(f"Skipping account with no currency: {acct}")
            continue

        # Parse account creation date to determine earliest possible month
        creation_time_str = acct.get("creationTime", "")
        if creation_time_str:
            try:
                created_dt = datetime.fromisoformat(
                    creation_time_str.replace("Z", "+00:00")
                )
                natural_start = (created_dt.year, created_dt.month)
            except (ValueError, TypeError):
                logger.warning(
                    "Could not parse creationTime '%s' for %s, defaulting to 12 months ago",
                    creation_time_str, currency,
                )
                fallback = today - timedelta(days=365)
                natural_start = (fallback.year, fallback.month)
        else:
            fallback = today - timedelta(days=365)
            natural_start = (fallback.year, fallback.month)

        # If since_date is provided, use its month as start (but never before the account
        # existed — the API has no data before account creation).
        if since_date is not None:
            requested_start = (since_date.year, since_date.month)
            # Use the LATER of requested_start and natural_start: the API has no
            # data from before the account existed
            start_month = max(requested_start, natural_start)
        else:
            start_month = natural_start

        months = _month_range(start_month, end_month)

        _log(f"{currency}: start_month={start_month}, end_month={end_month}, total_months={len(months)}")

        # Query DB for existing wise_{CURRENCY}_* source_files
        existing_source_files = db.get_source_files_by_prefix(f"wise_{currency}_")
        synced_months: set[tuple[int, int]] = set()
        for sf in existing_source_files:
            parts = sf.split("_")
            if len(parts) == 3 and len(parts[2]) == 6:
                try:
                    y = int(parts[2][:4])
                    m = int(parts[2][4:6])
                    synced_months.add((y, m))
                except ValueError:
                    pass

        _log(f"{currency}: already synced months: {sorted(synced_months)}, existing_source_files: {existing_source_files}")

        total_months = len(months)
        processed = 0

        for year, month in months:
            source_file = f"wise_{currency}_{year:04d}{month:02d}"
            is_current_month = (year == today.year and month == today.month)

            # Skip past months that are already synced
            if (year, month) in synced_months and not is_current_month:
                processed += 1
                _log(f"{source_file}: SKIP (past month, already synced)")
                continue

            # Current month: delete existing and re-fetch
            if is_current_month and (year, month) in synced_months:
                _log(f"{source_file}: DELETE + re-fetch (current month)")
                db.delete_transactions_by_source_file(source_file)

            processed += 1
            month_name = date(year, month, 1).strftime("%b")
            _log(
                f"Syncing Wise {currency} {month_name} {year}... "
                f"({processed}/{total_months})"
            )

            start_date = date(year, month, 1)
            end_date = min(_last_day_of_month(year, month), today)
            _log(f"{source_file}: fetching {start_date} to {end_date}")

            try:
                txns = fetch_wise_transactions(
                    token, profile_id,
                    currency=currency,
                    since=start_date,
                    until=end_date,
                    account_id=acct_id,
                )
            except Exception as e:
                _log(f"{source_file}: EXCEPTION from fetch: {e}")
                results[source_file] = {"error": str(e)}
                time.sleep(0.1)
                continue

            _log(f"{source_file}: fetch returned {len(txns)} transactions")

            if not txns:
                _log(f"{source_file}: EMPTY — no transactions in this period")
                results[source_file] = {"new": 0, "total": 0}
                time.sleep(0.1)
                continue

            new_count = 0
            fail_count = 0
            for txn in txns:
                txn.source_file = source_file
                try:
                    db.insert_transaction(txn)
                    new_count += 1
                except Exception as e:
                    fail_count += 1
                    _log(f"{source_file}: insert failed for row {txn.source_row}: {e}")

            _log(f"{source_file}: inserted {new_count}/{len(txns)} new transactions")
            if new_count == 0 and len(txns) > 0:
                _log(f"{source_file}: WARNING — all {len(txns)} inserts failed; "
                     f"this batch will not appear in the UI")
                results[source_file] = {
                    "new": 0,
                    "total": len(txns),
                    "error": f"All {len(txns)} inserts failed ({fail_count} errors)",
                }
            else:
                results[source_file] = {"new": new_count, "total": len(txns)}

            # Download the Wise PDF statement for this month and persist it to
            # the file store so users can later open the original file from
            # the Uploaded Files card on the Transactions page. Best-effort —
            # failure to fetch or upload must not affect the sync result.
            try:
                _store_wise_statement_pdf(
                    token=token,
                    profile_id=profile_id,
                    account_id=acct_id,
                    currency=currency,
                    start_date=start_date,
                    end_date=end_date,
                    source_file=source_file,
                    db=db,
                    user_id=user_id,
                    log=_log,
                )
            except Exception as e:
                _log(f"{source_file}: statement PDF archive skipped: {e}")
            time.sleep(0.1)  # Rate limit courtesy: 100ms between API calls

    _log(f"Final results: {results}")
    return results
