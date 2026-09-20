"""Apply pending SQL migrations in order.

Run this against the PostgreSQL cluster after ``db/schema_pg.sql`` and before
``db/local_grants.sql`` — see the Quickstart in README.md. Nothing injects the
environment, so this module loads ``.env`` itself.

The schema ships as a single baseline and ``db/migrations/`` starts empty, so a
fresh install has nothing to apply and this is a no-op that says so. It becomes
useful the moment somebody adds ``001_….sql``.

Invariants:
  * Each migration runs inside its own transaction; partial state never
    persists.
  * Applied migrations are recorded in ``schema_migrations(filename)``, which
    ``db/schema_pg.sql`` creates and records the baseline in.
  * Files are discovered in filename order; ``*.rollback.sql`` is ignored.
  * Idempotent: running twice with no new files is a no-op.

``MIGRATIONS_BASELINE=1`` records every pending file as applied *without*
executing it — for adopting a database that was brought up to date by hand.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys

import psycopg2
from dotenv import load_dotenv

# Nothing injects the environment — read it from .env, the same file
# config/settings.py reads, and on the same terms: the file wins over the
# shell (override=True).
load_dotenv(override=True)

logger = logging.getLogger("db.migrate")

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"


def _pending(cur, files: list[pathlib.Path]) -> list[pathlib.Path]:
    cur.execute("SELECT filename FROM schema_migrations")
    applied = {row[0] for row in cur.fetchall()}
    return [p for p in files if p.name not in applied]


def _discover() -> list[pathlib.Path]:
    files = [
        p for p in MIGRATIONS_DIR.glob("*.sql")
        if not p.name.endswith(".rollback.sql")
    ]
    return sorted(files, key=lambda p: p.name)


def run(dsn: str | None = None, baseline: bool | None = None) -> int:
    dsn = dsn or os.environ["DATABASE_URL"]
    baseline = baseline if baseline is not None else os.environ.get("MIGRATIONS_BASELINE") == "1"

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # Also created by db/schema_pg.sql. Created here too so the runner
            # works against a database that has not had the baseline applied.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename   TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        conn.commit()

        files = _discover()
        with conn.cursor() as cur:
            pending = _pending(cur, files)

        if not pending:
            logger.info("schema up to date (%d migrations, 0 pending)", len(files))
            return 0

        if baseline:
            with conn.cursor() as cur:
                for p in pending:
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)",
                        (p.name,),
                    )
            conn.commit()
            logger.info("baselined %d migrations without executing", len(pending))
            return 0

        for p in pending:
            logger.info("applying %s", p.name)
            sql = p.read_text(encoding="utf-8")
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)",
                        (p.name,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("migration failed: %s", p.name)
                raise

        logger.info("applied %d migrations", len(pending))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    sys.exit(run())
