from __future__ import annotations

from psycopg2.extras import Json

from .base import db_cursor


def _norm_username(name: str) -> str:
    return (name or "").strip().lstrip("@").lower()


def list_x_tracked_users() -> list[str]:
    with db_cursor() as cur:
        cur.execute("SELECT username FROM x_tracked_users ORDER BY username")
        return [r[0] for r in cur.fetchall()]


def add_x_tracked_users(usernames: list[str]) -> None:
    rows = [(u,) for u in {_norm_username(n) for n in usernames or []} if u]
    if not rows:
        return
    with db_cursor() as cur:
        cur.executemany(
            "INSERT INTO x_tracked_users (username) VALUES (%s) ON CONFLICT (username) DO NOTHING",
            rows,
        )


def remove_x_tracked_users(usernames: list[str]) -> None:
    rows = [(u,) for u in {_norm_username(n) for n in usernames or []} if u]
    if not rows:
        return
    with db_cursor() as cur:
        cur.executemany("DELETE FROM x_tracked_users WHERE username = %s", rows)


def insert_x_tweets(rows: list[tuple[str, str, int, dict]]) -> None:
    if not rows:
        return
    with db_cursor() as cur:
        cur.executemany(
            """
            INSERT INTO x_tweets (tweet_id, username, created_at, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tweet_id) DO NOTHING
            """,
            [
                (str(tid), _norm_username(user), int(created_at), Json(payload))
                for tid, user, created_at, payload in rows
            ],
        )


def max_x_tweet_row_id() -> int:
    with db_cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM x_tweets")
        return int(cur.fetchone()[0] or 0)


def list_x_tweets_after(row_id: int, limit: int = 100) -> list[tuple[int, int, dict]]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, created_at, payload FROM x_tweets WHERE id > %s ORDER BY id LIMIT %s",
            (int(row_id), int(limit)),
        )
        return [(int(r[0]), int(r[1] or 0), r[2]) for r in cur.fetchall()]


def list_x_recent_tweets(usernames: list[str] | None = None, limit: int = 50) -> list[dict]:
    names = [u for u in {_norm_username(n) for n in usernames or []} if u]
    with db_cursor() as cur:
        if names:
            cur.execute(
                """
                SELECT payload FROM x_tweets
                WHERE username = ANY(%s)
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (names, int(limit)),
            )
        else:
            cur.execute(
                "SELECT payload FROM x_tweets ORDER BY created_at DESC, id DESC LIMIT %s",
                (int(limit),),
            )
        return [r[0] for r in cur.fetchall()]


def claim_x_poll_leader(holder: str, ttl_seconds: int = 90) -> bool:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE x_poll_leader
            SET holder = %s, heartbeat_at = Now()
            WHERE id = 1
              AND (holder = %s OR holder IS NULL OR heartbeat_at < Now() - make_interval(secs => %s))
            RETURNING holder
            """,
            (holder, holder, int(ttl_seconds)),
        )
        return cur.fetchone() is not None
