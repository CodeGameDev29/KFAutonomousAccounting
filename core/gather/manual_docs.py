"""Manual document gathering — copies files from user-specified directories.

For documents no integration can fetch (a paper receipt photographed at the
till, a bank wire confirmation, a supplier PDF that only ever arrives by
post), users place files in a designated folder and this gatherer copies
them into the gather pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from core.gather.gmail import GatherResult

logger = logging.getLogger(__name__)


class ManualGatherer:
    """Copies documents from manual source directories into the gather pipeline."""

    def __init__(self, db, output_dir: str | Path) -> None:
        self.db = db
        self.output_dir = Path(output_dir) / "manual"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def gather(self, source_dirs: list[str | Path] | None = None) -> GatherResult:
        """Copy files from manual source directories.

        Args:
            source_dirs: List of directories to scan. If None, reads from
                         gather_sources config in the database.

        Returns:
            GatherResult with copy counts.
        """
        result = GatherResult(source="manual")

        if source_dirs is None:
            source_dirs = self._get_configured_dirs()

        if not source_dirs:
            return result

        supported = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".csv"}

        for src_dir in source_dirs:
            src_path = Path(src_dir)
            if not src_path.exists() or not src_path.is_dir():
                result.errors.append(f"Directory not found: {src_dir}")
                continue

            for f in sorted(src_path.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in supported:
                    continue

                source_id = f"manual:{f.name}"
                if self.db.get_gather_log_by_source_id("manual", source_id):
                    result.documents_duplicate += 1
                    continue

                try:
                    data = f.read_bytes()
                    file_hash = hashlib.sha256(data).hexdigest()

                    if self.db.get_gather_log_by_hash(file_hash) or self.db.is_file_processed(file_hash):
                        result.documents_duplicate += 1
                        continue

                    dest = self.output_dir / f.name
                    counter = 1
                    while dest.exists():
                        dest = self.output_dir / f"{f.stem}-{counter}{f.suffix}"
                        counter += 1

                    shutil.copy2(str(f), str(dest))

                    self.db.insert_gather_log(
                        source="manual",
                        source_id=source_id,
                        filename=dest.name,
                        file_hash=file_hash,
                        file_size=len(data),
                        stored_path=str(dest),
                        metadata_json={"original_path": str(f)},
                    )
                    result.documents_new += 1
                except Exception as e:
                    result.errors.append(f"Failed to copy {f.name}: {e}")

        return result

    def _get_configured_dirs(self) -> list[str]:
        """Read manual source directories from gather_sources config."""
        sources = self.db.get_gather_sources()
        for src in sources:
            if src["source_type"] == "manual" and src.get("config_json"):
                dirs = src["config_json"].get("source_dirs", [])
                if dirs:
                    return dirs
        return []
