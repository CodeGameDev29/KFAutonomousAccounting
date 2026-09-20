"""PostgreSQL connection pool and DatabasePg factory.

Reads DATABASE_URL from the environment — typically a PostgreSQL cluster on the
same machine — and provides a singleton connection pool with a convenient
factory function.

The pool connects as the cluster owner, but almost nothing actually runs with
those privileges: DatabasePg drops to the ``authenticated`` role for the
duration of every transaction so that row-level security is what enforces
tenant isolation. See db/database_pg.py and db/local_grants.sql.

Usage:
    from db.connection import get_database

    db = get_database(user_id="some-uuid")
    txns = db.get_unmatched_transactions()
"""

from __future__ import annotations

import logging
import os
import threading

import psycopg2.pool

from db.database_pg import DatabasePg

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return the singleton connection pool, creating it on first call.

    Reads DATABASE_URL from the environment. The pool is created once and
    reused for the lifetime of the process.

    Raises:
        KeyError: If DATABASE_URL is not set.
        psycopg2.OperationalError: If the database is unreachable.
    """
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        # Double-check after acquiring the lock: another thread may have
        # created the pool in the meantime.
        if _pool is not None:
            return _pool

        dsn = os.environ["DATABASE_URL"]
        logger.info("Creating PostgreSQL connection pool (minconn=1, maxconn=20)")
        # Note: DSN intentionally not logged to avoid credential exposure
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=dsn)
        return _pool


def get_database(user_id: str) -> DatabasePg:
    """Create a DatabasePg instance bound to a specific user.

    This is the primary entry point for request handlers. Each request
    should call this with the authenticated user's ID.

    Args:
        user_id: UUID string of the authenticated user (from JWT / auth middleware).

    Returns:
        A DatabasePg instance scoped to the given user.
    """
    return DatabasePg(get_pool(), user_id)


def close_pool() -> None:
    """Close all connections in the pool. Call during graceful shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            logger.info("Closing PostgreSQL connection pool")
            _pool.closeall()
            _pool = None
