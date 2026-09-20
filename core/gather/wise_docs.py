"""Wise document gathering — downloads statement PDFs for each currency account."""

from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta
from pathlib import Path

from core.gather.gmail import GatherResult
from core.wise_api import (
    download_wise_statement,
    get_borderless_accounts,
    get_profile_id,
    get_wise_token,
)

logger = logging.getLogger(__name__)


class WiseGatherer:
    """Downloads Wise statement PDFs and syncs transactions."""

    def __init__(self, credentials_store, db, output_dir: str | Path) -> None:
        self.credentials_store = credentials_store
        self.db = db
        self.output_dir = Path(output_dir) / "wise"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def is_connected(self) -> bool:
        """Check if a Wise API token is available."""
        token = self.credentials_store.retrieve("wise_api_token")
        if token:
            return True
        return get_wise_token() is not None

    def _get_token(self) -> str | None:
        """Get token from credential store or environment."""
        token = self.credentials_store.retrieve("wise_api_token")
        if token:
            return token
        return get_wise_token()

    def gather(self, since: date | None = None) -> GatherResult:
        """Download Wise statement PDFs for each currency account.

        Args:
            since: Start date for statements. Defaults to 30 days ago.

        Returns:
            GatherResult with counts.
        """
        result = GatherResult(source="wise")

        token = self._get_token()
        if not token:
            result.errors.append("Wise API token not configured")
            return result

        if since is None:
            since = date.today() - timedelta(days=30)

        profile_id = get_profile_id(token)
        if profile_id is None:
            result.errors.append("Could not get Wise profile ID")
            return result

        balances = get_borderless_accounts(token, profile_id)
        if not balances:
            result.errors.append("No Wise currency accounts found")
            return result

        end_date = date.today()

        for bal in balances:
            currency = bal.get("currency", "")
            account_id = bal.get("id")
            if not account_id or not currency:
                continue

            source_id = f"wise_statement_{currency}_{since}_{end_date}"

            # Dedup check
            existing = self.db.get_gather_log_by_source_id("wise", source_id)
            if existing:
                result.documents_duplicate += 1
                continue

            pdf_path = download_wise_statement(
                token=token,
                profile_id=profile_id,
                account_id=account_id,
                start_date=since,
                end_date=end_date,
                output_dir=str(self.output_dir),
                currency=currency,
            )

            if pdf_path and Path(pdf_path).exists():
                file_bytes = Path(pdf_path).read_bytes()
                file_hash = hashlib.sha256(file_bytes).hexdigest()

                # Hash-based dedup
                if self.db.get_gather_log_by_hash(file_hash) or self.db.is_file_processed(file_hash):
                    result.documents_duplicate += 1
                    continue

                self.db.insert_gather_log(
                    source="wise",
                    source_id=source_id,
                    filename=Path(pdf_path).name,
                    file_hash=file_hash,
                    file_size=len(file_bytes),
                    stored_path=pdf_path,
                    metadata_json={
                        "currency": currency,
                        "start_date": str(since),
                        "end_date": str(end_date),
                        "profile_id": profile_id,
                    },
                )
                result.documents_new += 1
            else:
                result.errors.append(f"Failed to download {currency} statement")

        # Also sync transactions through the Wise API client
        try:
            from core.wise_api import fetch_wise_transactions

            for currency_code in ["USD", "CAD"]:
                txns = fetch_wise_transactions(
                    token, profile_id, currency=currency_code, since=since, until=end_date
                )
                for txn in txns:
                    try:
                        self.db.insert_transaction(txn)
                        result.transactions_new += 1
                    except Exception:
                        result.transactions_duplicate += 1
        except Exception as e:
            result.errors.append(f"Wise transaction sync error: {e}")

        return result
