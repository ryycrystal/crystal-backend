from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from env_loader import load_env

load_env()


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


def _clean_text(value) -> str:
    if value is None:
        return ""
    s = str(value)
    return s.replace("\x00", "")


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


def close_pool() -> None:
    global _POOL

    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.closeall()
        _POOL = None


def _get_pool() -> ThreadedConnectionPool:
    global _POOL

    if _POOL is None:
        raise RuntimeError("[DB] Uninitialized connection pool")

    return _POOL


def acquire_indexer_lock() -> psycopg2.extensions.connection:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute("SELECT pg_try_advisory_lock(%s);", (_ADVISORY_LOCK_KEY,))
            row = cur.fetchone()
            if not row or not bool(row[0]):
                raise RuntimeError("[DB] Another indexer already holds the advisory lock")
        finally:
            cur.close()
        return conn
    except Exception:
        pool.putconn(conn)
        raise


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
