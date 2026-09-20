#!/usr/bin/env python
"""Recategorize every account's transactions with the fixed ISED/GIFI engine.

The bulk form of the "Recategorize All" button, for when the taxonomy or the
rules behind it have changed and every account should be re-derived rather
than one. For each account with transactions it clears the UNCONFIRMED
categories (a category the user confirmed is left alone), drops the vendor
cache, seeds the fixed taxonomy and runs the categorization agent over the
whole book.

Reads DATABASE_URL from .env and writes directly to that database.

Safety: the snapshot table ``transactions_pre_recategorize_backup`` is created
BEFORE any change, so the previous categories can be restored. Re-running keeps
the first snapshot rather than overwriting it.

    python scripts/recategorize_all_accounts.py --dry-run   # audit only
    python scripts/recategorize_all_accounts.py --apply
    python scripts/recategorize_all_accounts.py --apply --only <uuid>
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# Same terms as config/settings.py: .env wins over the shell.
load_dotenv(override=True)

from config.ised_categories import CATEGORY_NAME_SET  # noqa: E402


def _raw():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    return conn


def _label(user_id) -> str:
    """A short, stable tag for an account, for the progress output.

    A hash rather than the account id or its email address: the output of this
    script is the kind of thing that gets pasted into an issue, and it only
    ever needs to tell accounts apart.
    """
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:8]


def _dist(cur, user_id):
    cur.execute(
        "SELECT COALESCE(category,'<NULL>'), COUNT(*) FROM transactions "
        "WHERE user_id=%s GROUP BY 1 ORDER BY 2 DESC",
        (user_id,),
    )
    return dict(cur.fetchall())


def _noncanon(cur, user_id):
    cur.execute(
        "SELECT DISTINCT category FROM transactions "
        "WHERE user_id=%s AND category IS NOT NULL",
        (user_id,),
    )
    return sorted(c for (c,) in cur.fetchall() if c not in CATEGORY_NAME_SET)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="run the agent (default: dry-run audit)")
    ap.add_argument("--only", default=None, help="restrict to a single user UUID")
    args = ap.parse_args()

    conn = _raw()
    cur = conn.cursor()

    # Identify accounts.
    cur.execute(
        "SELECT t.user_id, COUNT(*), COUNT(*) FILTER (WHERE t.category_confirmed_by_user) "
        "FROM transactions t GROUP BY 1 ORDER BY 2 DESC"
    )
    accounts = cur.fetchall()

    print(f"{'ACCOUNTS':=^70}")
    for uid, n, c in accounts:
        print(f"  {str(uid):40s} txns={n:4d} confirmed={c}")

    if not args.apply:
        print("\n(dry-run) BEFORE distributions:")
        for uid, _n, _c in accounts:
            if args.only and str(uid) != args.only:
                continue
            print(f"\n  account {_label(uid)}:")
            for cat, k in _dist(cur, uid).items():
                print(f"    {cat:45s} {k}")
        print("\nRe-run with --apply to recategorize.")
        conn.close()
        return

    # Safety snapshot BEFORE any change (idempotent - keeps the first snapshot).
    cur.execute(
        "CREATE TABLE IF NOT EXISTS transactions_pre_recategorize_backup AS "
        "SELECT id, user_id, category, category_source, category_confidence, "
        "category_reasoning, category_confirmed_by_user FROM transactions"
    )
    print("\nsnapshot: transactions_pre_recategorize_backup ready")

    from core.categorization_agent import run_categorization_agent
    from db.connection import get_database

    for uid, n, c in accounts:
        uid = str(uid)
        if args.only and uid != args.only:
            continue
        print(f"\n{('recategorize account ' + _label(uid)):-^70}")
        db = get_database(uid)
        cleared = db.clear_all_transaction_categories()
        cache = db.clear_vendor_cache()
        db.seed_fixed_taxonomy()
        result = run_categorization_agent(db, uid, "all")
        after = _dist(cur, uid)
        nc = _noncanon(cur, uid)
        print(f"  cleared={cleared} vendor_cache_cleared={cache} "
              f"status={result.status} rule_locked={result.rule_locked} "
              f"vendors={result.vendors_categorized} refined={result.txns_refined} "
              f"llm_calls={result.llm_calls}")
        print("  AFTER categories: " + ", ".join(f"{k}={v}" for k, v in sorted(after.items())))
        if nc:
            print(f"  ** non-canonical remaining (confirmed/locked): {nc}")
        else:
            print("  all categories canonical or NULL [OK]")

    # Final global invariant check.
    cur.execute(
        "SELECT COUNT(*) FROM transactions t WHERE t.category IS NOT NULL "
        "AND t.category NOT IN %s",
        (tuple(CATEGORY_NAME_SET),),
    )
    leaks = cur.fetchone()[0]
    print(f"\n{'DONE':=^70}")
    print(f"global non-canonical non-NULL rows remaining: {leaks} "
          f"(expected: only values a user confirmed by hand)")
    conn.close()


if __name__ == "__main__":
    main()
