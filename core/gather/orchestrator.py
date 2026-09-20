"""Gather orchestrator — coordinates gathering from all configured sources."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from core.credentials import CredentialStore
from core.gather.gmail import GatherResult, GmailGatherer
from core.gather.manual_docs import ManualGatherer
from core.gather.paypal_docs import PayPalGatherer
from core.gather.wise_docs import WiseGatherer
from db.database import Database

logger = logging.getLogger(__name__)


class GatherOrchestrator:
    """Coordinates gathering from all configured sources."""

    def __init__(
        self,
        db: Database,
        credentials_store: CredentialStore,
        output_dir: str | Path = "gather",
    ) -> None:
        self.db = db
        self.credentials_store = credentials_store
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Mirror of everything gathered, in a second place a human can browse.
        # Defaults to ``<repo>/data/gather-mirror`` (gitignored) so the path
        # resolves with no configuration on any OS; set GATHER_MIRROR_DIR to
        # put it somewhere else (e.g. a Downloads folder or a synced drive), or
        # to "" to disable the mirror.
        mirror = os.environ.get("GATHER_MIRROR_DIR")
        if mirror is None:
            mirror = str(Path(__file__).resolve().parents[2] / "data" / "gather-mirror")
        self.downloads_dir = Path(mirror) if mirror else None
        if self.downloads_dir is not None:
            self.downloads_dir.mkdir(parents=True, exist_ok=True)

        # Initialize gatherers
        self.gmail = GmailGatherer(credentials_store, db, self.output_dir)
        self.manual = ManualGatherer(db, self.output_dir)
        self.paypal = PayPalGatherer(db, self.output_dir)
        self.wise = WiseGatherer(credentials_store, db, self.output_dir)

    def _is_source_enabled(self, source_type: str) -> bool:
        """Check if a source is enabled in the database."""
        sources = self.db.get_gather_sources()
        for src in sources:
            if src["source_type"] == source_type:
                return src["enabled"]
        return False

    async def gather_all(
        self,
        since: date | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> dict[str, GatherResult]:
        """Run all enabled sources. Each source is independent.

        Args:
            since: Gather data since this date. Defaults to 30 days ago.
            progress_callback: Optional callback(source, status) for progress.

        Returns:
            Dict mapping source_type to GatherResult.
        """
        if since is None:
            since = date.today() - timedelta(days=30)

        since_dt = datetime(since.year, since.month, since.day)
        results: dict[str, GatherResult] = {}

        sources_to_run = []
        if self._is_source_enabled("gmail") and self.gmail.is_connected():
            sources_to_run.append(("gmail", self._gather_gmail, since_dt))
        if self._is_source_enabled("paypal") and self.paypal.is_connected():
            sources_to_run.append(("paypal", self._gather_paypal, since))
        if self._is_source_enabled("wise") and self.wise.is_connected():
            sources_to_run.append(("wise", self._gather_wise, since))
        if self._is_source_enabled("manual"):
            sources_to_run.append(("manual", self._gather_manual, None))

        for source_type, gather_fn, since_arg in sources_to_run:
            if progress_callback:
                await progress_callback(source_type, "running")
            try:
                result = gather_fn(since_arg)
                results[source_type] = result
                self.db.update_gather_source_last_gathered(source_type)
                self._copy_to_downloads(source_type)
                if progress_callback:
                    await progress_callback(source_type, "complete")
            except Exception as e:
                logger.exception("Gather failed for %s", source_type)
                results[source_type] = GatherResult(
                    source=source_type, errors=[str(e)]
                )
                if progress_callback:
                    await progress_callback(source_type, "error")

        return results

    def _gather_gmail(self, since: datetime | None) -> GatherResult:
        return self.gmail.gather(since)

    def _gather_paypal(self, since: date | None) -> GatherResult:
        return self.paypal.gather(since)

    def _gather_wise(self, since: date | None) -> GatherResult:
        return self.wise.gather(since)

    def _gather_manual(self, _: None) -> GatherResult:
        return self.manual.gather()

    async def gather_source(
        self, source_type: str, since: date | None = None
    ) -> GatherResult:
        """Run a single source."""
        if since is None:
            since = date.today() - timedelta(days=30)
        since_dt = datetime(since.year, since.month, since.day)

        if source_type == "gmail":
            result = self.gmail.gather(since_dt)
        elif source_type == "paypal":
            result = self.paypal.gather(since)
        elif source_type == "wise":
            result = self.wise.gather(since)
        elif source_type == "manual":
            result = self.manual.gather()
        else:
            return GatherResult(source=source_type, errors=[f"Unknown source: {source_type}"])

        self.db.update_gather_source_last_gathered(source_type)
        self._copy_to_downloads(source_type)
        return result

    def _copy_to_downloads(self, source_type: str) -> None:
        """Mirror gathered files to ``GATHER_MIRROR_DIR/{source}/``."""
        if self.downloads_dir is None:
            return
        source_dir = self.output_dir / source_type
        if not source_dir.exists():
            return
        dest_dir = self.downloads_dir / source_type
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in source_dir.iterdir():
            if f.is_file():
                dest = dest_dir / f.name
                if not dest.exists():
                    shutil.copy2(str(f), str(dest))
                    logger.info("Copied to the gather mirror: %s", dest.name)

    def get_status(self) -> dict:
        """Return status of all sources."""
        sources = self.db.get_gather_sources()
        stats = self.db.get_gather_log_stats()

        status = {}
        for src in sources:
            st = src["source_type"]
            source_stats = stats.get(st, {})
            gathered = source_stats.get("GATHERED", 0)
            renamed = source_stats.get("RENAMED", 0)
            processed = source_stats.get("PROCESSED", 0)
            duplicates = source_stats.get("DUPLICATE", 0)

            connected = False
            if st == "gmail":
                connected = self.gmail.is_connected()
            elif st == "paypal":
                connected = self.paypal.is_connected()
            elif st == "wise":
                connected = self.wise.is_connected()
            elif st == "manual":
                connected = True

            status[st] = {
                "enabled": src["enabled"],
                "connected": connected,
                "last_gathered": src["last_gathered"],
                "documents_gathered": gathered + renamed + processed,
                "documents_duplicate": duplicates,
            }

        return status
