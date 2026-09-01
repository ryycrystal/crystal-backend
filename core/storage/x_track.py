from __future__ import annotations

import re

from psycopg2.extras import Json

from .base import db_cursor

_SOCIAL_HOSTS = ("x.com/", "twitter.com/")
_HANDLE_RE = re.compile(r"^[a-z0-9_]{1,15}$")
_RESERVED_HANDLES = frozenset({"i", "home", "intent", "search", "share", "hashtag", "explore", "messages"})


def _norm_username(name: str) -> str:
    return (name or "").strip().lstrip("@").lower()


def handle_from_social_url(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    for host in _SOCIAL_HOSTS:
        idx = s.find(host)
        if idx < 0:
            continue
        rest = s[idx + len(host) :].split("?")[0].split("#")[0].strip("/")
        if not rest or "/" in rest:
            return None
        if rest in _RESERVED_HANDLES or not _HANDLE_RE.match(rest):
            return None
        return rest
    return None


def list_token_social_handles() -> set[str]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT social1, social2, social3, social4
            FROM launchpad_tokens
            WHERE social1 <> '' OR social2 <> '' OR social3 <> '' OR social4 <> ''
            """
        )
        rows = cur.fetchall()
    out: set[str] = set()
    for row in rows:
        for raw in row:
            handle = handle_from_social_url(raw)
            if handle:
                out.add(handle)
    return out


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


def list_user_tracked(key: str) -> list[str]:
    with db_cursor() as cur:
        cur.execute("SELECT username FROM x_user_tracked WHERE key = %s ORDER BY username", (key,))
        return [r[0] for r in cur.fetchall()]


def count_user_tracked(key: str) -> int:
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM x_user_tracked WHERE key = %s", (key,))
        return int(cur.fetchone()[0] or 0)


def add_user_tracked(key: str, usernames: list[str]) -> None:
    rows = [(key, u) for u in {_norm_username(n) for n in usernames or []} if u]
    if not rows:
        return
    with db_cursor() as cur:
        cur.executemany(
            "INSERT INTO x_user_tracked (key, username) VALUES (%s, %s) ON CONFLICT (key, username) DO NOTHING",
            rows,
        )


def remove_user_tracked(key: str, usernames: list[str]) -> None:
    rows = [(key, u) for u in {_norm_username(n) for n in usernames or []} if u]
    if not rows:
        return
    with db_cursor() as cur:
        cur.executemany("DELETE FROM x_user_tracked WHERE key = %s AND username = %s", rows)


def list_polled_usernames() -> list[str]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT username FROM x_tracked_users
            UNION
            SELECT username FROM x_user_tracked
            ORDER BY username
            """
        )
        return [r[0] for r in cur.fetchall()]


def count_polled_usernames() -> int:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT username FROM x_tracked_users
                UNION
                SELECT username FROM x_user_tracked
            ) u
            """
        )
        return int(cur.fetchone()[0] or 0)


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


def held_tokens_with_handles(wallets: list[str]) -> list[dict]:
    addrs = [str(w or "").lower() for w in (wallets or []) if w]
    if not addrs:
        return []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT t.token, t.symbol, t.name, t.metadata_cid, t.source, t.last_price_native,
                   t.migrated, t.circulating_supply, t.social1, t.social2, t.social3, t.social4
            FROM launchpad_positions p
            JOIN launchpad_tokens t ON t.token = p.token
            WHERE p.user_address = ANY(%s) AND p.balance_token > 0
              AND (t.social1 <> '' OR t.social2 <> '' OR t.social3 <> '' OR t.social4 <> '')
            """,
            (addrs,),
        )
        rows = cur.fetchall()
    out = []
    for token, symbol, name, cid, source, price, migrated, supply, s1, s2, s3, s4 in rows:
        handle = next((h for h in (handle_from_social_url(x) for x in (s1, s2, s3, s4)) if h), None)
        if not handle:
            continue
        out.append(
            {
                "token": token,
                "handle": handle,
                "symbol": symbol or "",
                "name": name or "",
                "metadata_cid": cid or "",
                "source": int(source or 0),
                "last_price_native": str(price or 0),
                "migrated": bool(migrated),
                "circulating_supply": str(int(supply or 0)),
            }
        )
    return out
