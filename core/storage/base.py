from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from env_loader import load_env

load_env()


# assemble the connection url from DATABASE_URL or the PG parts
def _build_database_url() -> str | None:
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    host = os.getenv("PGHOST")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    database = os.getenv("PGDATABASE")
    port = os.getenv("PGPORT", "5432")
    sslmode = os.getenv("PGSSLMODE", "require")

    if host and user and password and database:
        user_q = quote(user, safe="")
        password_q = quote(password, safe="")
        database_q = quote(database, safe="")
        return f"postgresql://{user_q}:{password_q}@{host}:{port}/{database_q}?sslmode={sslmode}"

    return None


_DATABASE_URL: str | None = _build_database_url()
_DB_MIN_CONN: int = int(os.getenv("DB_MIN_CONN", "1"))
_DB_MAX_CONN: int = int(os.getenv("DB_MAX_CONN", "25"))

_POOL: ThreadedConnectionPool | None = None
_POOL_LOCK = threading.Lock()
_ADVISORY_LOCK_KEY: int = 18910274772340076


# strip nulls and whitespace so text is safe to store
def _clean_text(value) -> str:
    if value is None:
        return ""
    s = str(value)
    return s.replace("\x00", "")


# create the shared threaded connection pool
def init_pool() -> None:
    global _POOL

    if _DATABASE_URL is None:
        raise RuntimeError("[DB] Missing DB URL")

    with _POOL_LOCK:
        if _POOL is not None:
            return

        _POOL = ThreadedConnectionPool(
            minconn=_DB_MIN_CONN,
            maxconn=_DB_MAX_CONN,
            dsn=_DATABASE_URL,
        )


# close every pooled connection and drop the pool
def close_pool() -> None:
    global _POOL

    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.closeall()
        _POOL = None


# one standalone autocommit connection outside the pool, for LISTEN/NOTIFY where
# a pooled connection must not be parked on a blocking select
def listen_connection() -> psycopg2.extensions.connection:
    if _DATABASE_URL is None:
        raise RuntimeError("[DB] Missing DB URL")
    conn = psycopg2.connect(dsn=_DATABASE_URL)
    conn.autocommit = True
    return conn


# return the pool creating it on first use
def _get_pool() -> ThreadedConnectionPool:
    global _POOL

    if _POOL is None:
        raise RuntimeError("[DB] Uninitialized connection pool")

    return _POOL


# take the postgres advisory lock so only one indexer runs at a time
def acquire_indexer_lock(wait_seconds: float = 90.0) -> psycopg2.extensions.connection:
    pool = _get_pool()
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    attempt = 0

    while True:
        attempt += 1
        conn = pool.getconn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            try:
                cur.execute("SELECT pg_try_advisory_lock(%s);", (_ADVISORY_LOCK_KEY,))
                row = cur.fetchone()
                got = bool(row and row[0])
            finally:
                cur.close()
            if got:
                if attempt > 1:
                    print(f"[DB] advisory lock acquired after {attempt} attempts", flush=True)
                return conn
        except Exception:
            pool.putconn(conn)
            raise

        pool.putconn(conn)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"[DB] Another indexer still holds the advisory lock after {wait_seconds:g}s")
        print(
            f"[DB] advisory lock held by another indexer, retrying ({remaining:.0f}s left)",
            flush=True,
        )
        time.sleep(min(3.0, max(remaining, 0.1)))


# release the advisory lock and return its connection to the pool
def release_indexer_lock(conn: psycopg2.extensions.connection | None) -> None:
    if conn is None:
        return
    pool = _get_pool()
    try:
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT pg_advisory_unlock(%s);", (_ADVISORY_LOCK_KEY,))
            finally:
                cur.close()
        except Exception:
            pass
    finally:
        pool.putconn(conn)


@contextmanager
# pooled cursor that commits on success and rolls back on error
def db_cursor() -> Iterator[psycopg2.extensions.cursor]:
    pool = _get_pool()
    conn = pool.getconn()

    try:
        if conn.autocommit:
            conn.autocommit = False

        cur = conn.cursor()

        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        pool.putconn(conn)
