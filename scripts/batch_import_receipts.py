#!/usr/bin/env python3
"""Batch import receipts directly into the database.

No upload UI involved — reads receipt files straight off the filesystem,
extracts data through the normal LLM extraction chain, and inserts into the
documents table. Used for initial data population or bulk imports.

The directories to walk are given on the command line; there is no default
location:

    python scripts/batch_import_receipts.py path/to/receipts [more/dirs ...]
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import SUPPORTED_RECEIPT_EXTENSIONS, app_config
from core.extraction import build_document_from_extraction, compute_file_hash, extract_document
from db.database import Database


async def main(dirs):
    db = Database(str(app_config.db_path))

    files_to_process = []
    for dir_path in dirs:
        if not dir_path.exists():
            print(f"SKIP: {dir_path} does not exist")
            continue
        for f in sorted(dir_path.iterdir()):
            if f.suffix.lower() in SUPPORTED_RECEIPT_EXTENSIONS:
                files_to_process.append(f)

    print(f"Found {len(files_to_process)} receipt files to process")
    print()

    success = 0
    skipped = 0
    failed = 0

    for i, file_path in enumerate(files_to_process, 1):
        file_hash = compute_file_hash(file_path)

        if db.is_file_processed(file_hash):
            print(f"[{i}/{len(files_to_process)}] SKIP (duplicate): {file_path.name}")
            skipped += 1
            continue

        print(f"[{i}/{len(files_to_process)}] Processing: {file_path.name}...", end=" ", flush=True)

        try:
            extraction_data = await extract_document(file_path)

            doc = build_document_from_extraction(
                extraction_data=extraction_data,
                original_filename=file_path.name,
                file_hash=file_hash,
                raw_json=str(extraction_data),
            )
            # Use the file's existing path as stored_path
            doc.stored_path = str(file_path)

            db.insert_document(doc)
            try:
                db.insert_processed_file(file_hash, file_path.name)
            except Exception:
                pass

            confidence = doc.extraction_confidence
            vendor = doc.vendor or "Unknown"
            total = doc.total or "?"
            currency = doc.currency or "?"
            print(f"OK ({confidence}) — {vendor}, ${total} {currency}")
            success += 1

        except Exception as e:
            print(f"FAILED: {e}")
            failed += 1

    print()
    print(f"Results: {success} extracted, {skipped} skipped (duplicates), {failed} failed")
    print(f"Total documents in DB: {len(db.get_unmatched_documents()) + success}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract every receipt in the given directories into the documents table."
    )
    parser.add_argument(
        "dirs",
        nargs="+",
        type=Path,
        metavar="DIR",
        help="Directory of receipt files to import (repeatable).",
    )
    args = parser.parse_args()

    asyncio.run(main(args.dirs))
