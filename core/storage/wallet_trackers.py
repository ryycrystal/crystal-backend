from __future__ import annotations

from .base import db_cursor


def get_wallet_tracker_payload(key: str) -> dict | None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT version, iv, ciphertext, updated_at
            FROM wallet_tracker_payloads
            WHERE key = %s
            """,
            (key,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "version": int(row[0]),
        "iv": str(row[1]),
        "ciphertext": str(row[2]),
        "updatedAt": int(row[3]),
    }


def put_wallet_tracker_payload(
    key: str,
    version: int,
    iv: str,
    ciphertext: str,
    updated_at: int,
) -> bool:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO wallet_tracker_payloads (key, version, iv, ciphertext, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (key) DO UPDATE
            SET version = EXCLUDED.version,
                iv = EXCLUDED.iv,
                ciphertext = EXCLUDED.ciphertext,
                updated_at = EXCLUDED.updated_at
            WHERE wallet_tracker_payloads.updated_at <= EXCLUDED.updated_at
            """,
            (key, version, iv, ciphertext, updated_at),
        )
        return cur.rowcount > 0
