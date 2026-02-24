from __future__ import annotations
from typing import Optional, Iterator
from contextlib import contextmanager
from decimal import Decimal

import os
import threading
import psycopg2

from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import Json, execute_values

_DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL", "postgresql://postgres:ShIsCu2024;1@localhost:5432/logs")
_DB_MIN_CONN: int = 5
_DB_MAX_CONN: int = 125

_POOL: Optional[ThreadedConnectionPool] = None
_POOL_LOCK = threading.Lock()


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
    

