from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from psycopg2.extras import Json, execute_values

from core.ledger.types import (
    EVERY_TOKEN,
    FOLD_COLUMNS,
    POSITION_TEXT_COLUMNS,
    Flow,
    PositionRow,
    TokenReg,
    TraceResult,
    TxMeta,
)

FLOW_COLUMNS = tuple(Flow.__dataclass_fields__)
POSITION_COLUMNS = tuple(PositionRow.__dataclass_fields__)
PARKED_COLUMNS = (
    "observed_tokens",
    "estimated_tokens",
    "unresolved_tokens",
    "observed_basis",
    "estimated_basis",
)
TX_META_COLUMNS = ("txhash", "block_number", "tx_index", "from_addr", "to_addr", "value", "selector")
REGISTRY_COLUMNS = ("token", "source", "registered_block", "quote_token", "decimals", "active")

_FLOW_WEI = {"token_delta", "quote_delta", *FOLD_COLUMNS}
_FLOW_DECIMAL = {"mon_value", "usd_value", "price_native"}
_FLOW_ADDRESS = {"txhash", "wallet", "token", "quote_asset", "venue", "counterparty", "origin"}
_KEY_CHUNK = 500
_PAGE_SIZE = 1000

_INSERT_FLOWS_SQL = (
    f"INSERT INTO wallet_flows ({', '.join(FLOW_COLUMNS)}) VALUES %s "
    "ON CONFLICT (block_number, tx_index, log_index, sub_index) DO NOTHING RETURNING 1"
)
_SELECT_FLOWS_SQL = (
    f"SELECT {', '.join(FLOW_COLUMNS)} FROM wallet_flows "
    "WHERE (wallet, token) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
    "ORDER BY block_number, tx_index, log_index, sub_index"
)
_UPSERT_POSITIONS_SQL = (
    f"INSERT INTO positions_v2 ({', '.join(POSITION_COLUMNS)}) VALUES %s "
    "ON CONFLICT (wallet, token) DO UPDATE SET " + ", ".join(f"{col} = EXCLUDED.{col}" for col in POSITION_COLUMNS[2:])
)
_UPDATE_FOLD_DELTAS_SQL = (
    "UPDATE wallet_flows AS f SET " + ", ".join(f"{col} = v.{col}" for col in FOLD_COLUMNS) + " "
    f"FROM (VALUES %s) AS v(block_number, tx_index, log_index, sub_index, {', '.join(FOLD_COLUMNS)}) "
    "WHERE f.block_number = v.block_number AND f.tx_index = v.tx_index "
    "AND f.log_index = v.log_index AND f.sub_index = v.sub_index"
)
_UPDATE_FOLD_DELTAS_TEMPLATE = (
    "(%s::bigint, %s::int, %s::int, %s::int, " + ", ".join(["%s::numeric"] * len(FOLD_COLUMNS)) + ")"
)
_SELECT_TX_META_SQL = f"SELECT {', '.join(TX_META_COLUMNS)} FROM tx_meta WHERE txhash = ANY(%s)"
_UPSERT_TX_META_SQL = (
    f"INSERT INTO tx_meta ({', '.join(TX_META_COLUMNS)}) VALUES %s "
    "ON CONFLICT (txhash) DO UPDATE SET " + ", ".join(f"{col} = EXCLUDED.{col}" for col in TX_META_COLUMNS[1:])
)
_SELECT_REGISTRY_SQL = f"SELECT {', '.join(REGISTRY_COLUMNS)} FROM token_registry"
_UPSERT_REGISTRY_SQL = (
    "INSERT INTO token_registry (token, source, registered_block, quote_token, decimals, active) "
    "VALUES (%s, %s, %s, %s, COALESCE(%s, 18), TRUE) "
    "ON CONFLICT (token) DO UPDATE SET "
    "source = EXCLUDED.source, "
    "registered_block = LEAST(token_registry.registered_block, EXCLUDED.registered_block), "
    "quote_token = COALESCE(EXCLUDED.quote_token, token_registry.quote_token), "
    "decimals = EXCLUDED.decimals, active = TRUE "
    f"RETURNING {', '.join(REGISTRY_COLUMNS)}"
)

_REGISTRY: dict[str, TokenReg] | None = None


def _lower(value):
    if isinstance(value, str):
        return value.lower()
    return value


def _wei(value):
    if value is None:
        return None
    return int(value)


def _decimal(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def _flow_value(flow: Flow, column: str):
    value = getattr(flow, column)
    if column in _FLOW_WEI:
        return _wei(value)
    if column in _FLOW_DECIMAL:
        return _decimal(value)
    if column in _FLOW_ADDRESS:
        return _lower(value)
    return value


def _flow_from_row(row) -> Flow:
    values = {}
    for column, value in zip(FLOW_COLUMNS, row):
        if column in _FLOW_WEI:
            value = _wei(value)
        elif column in _FLOW_DECIMAL:
            value = _decimal(value)
        values[column] = value
    return Flow(**values)


def _flow_pk(flow: Flow) -> tuple[int, int, int, int]:
    return (int(flow.block_number), int(flow.tx_index), int(flow.log_index), int(flow.sub_index))


def insert_flows(cur, flows: list[Flow]) -> int:
    if not flows:
        return 0
    rows = [tuple(_flow_value(flow, column) for column in FLOW_COLUMNS) for flow in flows]
    inserted = execute_values(cur, _INSERT_FLOWS_SQL, rows, page_size=_PAGE_SIZE, fetch=True)
    return len(inserted)


def load_flows(cur, keys: list[tuple[str, str]]) -> dict[tuple[str, str], list[Flow]]:
    out: dict[tuple[str, str], list[Flow]] = {(_lower(wallet), _lower(token)): [] for wallet, token in keys}
    pairs = list(out)
    for start in range(0, len(pairs), _KEY_CHUNK):
        chunk = pairs[start : start + _KEY_CHUNK]
        cur.execute(_SELECT_FLOWS_SQL, ([wallet for wallet, _ in chunk], [token for _, token in chunk]))
        for row in cur.fetchall():
            flow = _flow_from_row(row)
            out[(flow.wallet, flow.token)].append(flow)
    return out


def _position_value(row: PositionRow, column: str):
    value = getattr(row, column)
    if column in POSITION_TEXT_COLUMNS:
        return _lower(value)
    if value is None:
        return None
    return int(value)


def upsert_positions(cur, rows: list[PositionRow]) -> None:
    if not rows:
        return
    by_key = {(_lower(row.wallet), _lower(row.token)): row for row in rows}
    values = [tuple(_position_value(row, column) for column in POSITION_COLUMNS) for row in by_key.values()]
    execute_values(cur, _UPSERT_POSITIONS_SQL, values, page_size=_PAGE_SIZE)


def _position_row(state, wallet: str, token: str) -> PositionRow:
    if isinstance(state, PositionRow):
        row = state
    elif hasattr(state, "to_row"):
        row = state.to_row()
    else:
        row = PositionRow(**{column: getattr(state, column, None) for column in POSITION_COLUMNS})
    return replace(row, wallet=wallet, token=token)


def _write_fold_deltas(cur, updates: list[tuple]) -> None:
    if not updates:
        return
    execute_values(cur, _UPDATE_FOLD_DELTAS_SQL, updates, template=_UPDATE_FOLD_DELTAS_TEMPLATE, page_size=_PAGE_SIZE)


def _fold_values(flow: Flow) -> tuple[int, ...]:
    return tuple(_wei(getattr(flow, column, None)) or 0 for column in FOLD_COLUMNS)


def load_parked(cur, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """The per-venue parked buckets, without which a stored position is not a resumable checkpoint."""
    from core.ledger.fold import Parked

    out: dict[tuple[str, str], dict] = {}
    pairs = sorted({(_lower(wallet), _lower(token)) for wallet, token in keys})
    for start in range(0, len(pairs), _KEY_CHUNK):
        chunk = pairs[start : start + _KEY_CHUNK]
        cur.execute(
            f"SELECT wallet, token, venue, {', '.join(PARKED_COLUMNS)} FROM parked_entitlements "
            "WHERE (wallet, token) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            ([wallet for wallet, _ in chunk], [token for _, token in chunk]),
        )
        for wallet, token, venue, *values in cur.fetchall():
            bucket = Parked(**dict(zip(PARKED_COLUMNS, (int(value) for value in values))))
            out.setdefault((wallet, token), {})[venue] = bucket
    return out


def _write_parked(cur, keys: list[tuple[str, str]], parked: list[tuple]) -> None:
    if not keys:
        return
    for start in range(0, len(keys), _KEY_CHUNK):
        chunk = keys[start : start + _KEY_CHUNK]
        cur.execute(
            "DELETE FROM parked_entitlements WHERE (wallet, token) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            ([wallet for wallet, _ in chunk], [token for _, token in chunk]),
        )
    if parked:
        execute_values(
            cur,
            f"INSERT INTO parked_entitlements (wallet, token, venue, {', '.join(PARKED_COLUMNS)}) VALUES %s",
            parked,
            page_size=_PAGE_SIZE,
        )


def load_token_flows(cur, token: str, after: int | None = None) -> list[Flow]:
    sql = f"SELECT {', '.join(FLOW_COLUMNS)} FROM wallet_flows WHERE token = %s"
    params: tuple = (_lower(token),)
    if after is not None:
        sql += " AND block_number > %s"
        params += (int(after),)
    cur.execute(sql + " ORDER BY block_number, tx_index, log_index, sub_index", params)
    return [_flow_from_row(row) for row in cur.fetchall()]


def load_positions(cur, token: str, wallets) -> dict:
    """Rebuild each wallet's fold state from what was stored, so a fold can resume instead of starting over."""
    from core.ledger.fold import PositionState

    addrs = sorted({_lower(wallet) for wallet in wallets if wallet})
    if not addrs:
        return {}
    cur.execute(
        f"SELECT {', '.join(POSITION_COLUMNS)} FROM positions_v2 WHERE token = %s AND wallet = ANY(%s)",
        (_lower(token), addrs),
    )
    rows = [PositionRow(**dict(zip(POSITION_COLUMNS, row))) for row in cur.fetchall()]
    parked = load_parked(cur, [(row.wallet, row.token) for row in rows])
    return {row.wallet: PositionState.from_row(row, parked.get((row.wallet, row.token))) for row in rows}


def fold_watermarks(cur, tokens) -> dict[str, int]:
    addrs = sorted({_lower(token) for token in tokens if token})
    if not addrs:
        return {}
    cur.execute("SELECT token, folded_through FROM token_fold_state WHERE token = ANY(%s)", (addrs,))
    return {token: int(block) for token, block in cur.fetchall()}


def set_fold_watermark(cur, token: str, block: int) -> None:
    cur.execute(
        "INSERT INTO token_fold_state (token, folded_through) VALUES (%s, %s) "
        "ON CONFLICT (token) DO UPDATE SET folded_through = EXCLUDED.folded_through",
        (_lower(token), int(block)),
    )


def _write_fold(cur, token: str, states: dict, folded: list[Flow], stored: dict, full: bool) -> None:
    if full:
        cur.execute("DELETE FROM positions_v2 WHERE token = %s", (_lower(token),))
        cur.execute("DELETE FROM parked_entitlements WHERE token = %s", (_lower(token),))
    rows = [_position_row(state, wallet, token) for wallet, state in states.items()]
    keys = [(wallet, token) for wallet in states]
    parked = [
        (wallet, token, venue, *(int(getattr(bucket, name)) for name in PARKED_COLUMNS))
        for wallet, state in states.items()
        for venue, bucket in getattr(state, "parked", {}).items()
    ]
    updates = []
    for flow in folded:
        before = stored.get(_flow_pk(flow))
        values = _fold_values(flow)
        if before is None or _fold_values(before) != values:
            updates.append((*_flow_pk(flow), *values))
    upsert_positions(cur, rows)
    _write_fold_deltas(cur, updates)
    _write_parked(cur, [] if full else keys, parked)


def refold_tokens(cur, touched: dict[str, int], fold_fn) -> int:
    """Fold each touched token in chain order, resuming from its checkpoint where the new flows allow it.

    A flow landing at or below the watermark means history changed underneath the checkpoint, so that token
    is folded from the beginning; otherwise only the wallets in the new flows are loaded and advanced. The
    full pass is what makes a correction possible at all, and keeping it off the common path is what makes
    the ordered fold affordable.
    """
    watermarks = fold_watermarks(cur, touched)
    written = 0
    for token in sorted(touched):
        watermark = watermarks.get(token)
        full = watermark is None or int(touched[token]) <= watermark
        flows = load_token_flows(cur, token, None if full else watermark)
        if not flows:
            if full:
                cur.execute("DELETE FROM positions_v2 WHERE token = %s", (_lower(token),))
                cur.execute("DELETE FROM parked_entitlements WHERE token = %s", (_lower(token),))
            continue
        previous = None if full else load_positions(cur, token, {flow.wallet for flow in flows})
        states, folded = fold_fn(previous, flows)
        _write_fold(cur, token, states, folded, {_flow_pk(flow): flow for flow in flows}, full)
        set_fold_watermark(cur, token, max(int(flow.block_number) for flow in flows))
        written += len(states)
    return written


def delete_token_flows(cur, tokens, block: int) -> list[tuple[str, str]]:
    """Drop what any earlier run wrote for these tokens in this block, returning the positions it touched."""
    addrs = sorted({_lower(token) for token in tokens if token})
    if not addrs:
        return []
    cur.execute(
        "DELETE FROM wallet_flows WHERE block_number = %s AND token = ANY(%s) RETURNING wallet, token",
        (int(block), addrs),
    )
    return [(wallet, token) for wallet, token in cur.fetchall()]


def extend_coverage(cur, token: str | None, from_block: int, to_block: int) -> None:
    """Record that every movement of this token in [from_block, to_block] is in wallet_flows.

    None means every registered token, which is what the live indexer sees. Touching or overlapping ranges
    merge into one row, so a token replayed from creation and then followed live holds a single span.
    """
    key = _lower(token) or EVERY_TOKEN
    lo, hi = int(from_block), int(to_block)
    if lo > hi:
        return
    cur.execute(
        "SELECT from_block, to_block FROM token_coverage "
        "WHERE token = %s AND from_block <= %s + 1 AND to_block >= %s - 1 ORDER BY from_block",
        (key, hi, lo),
    )
    rows = cur.fetchall()
    if len(rows) == 1 and int(rows[0][0]) <= lo and int(rows[0][1]) >= hi:
        return
    for a, b in rows:
        lo = min(lo, int(a))
        hi = max(hi, int(b))
    if len(rows) == 1:
        cur.execute(
            "UPDATE token_coverage SET from_block = %s, to_block = %s WHERE token = %s AND from_block = %s",
            (lo, hi, key, int(rows[0][0])),
        )
        return
    if rows:
        cur.execute(
            "DELETE FROM token_coverage WHERE token = %s AND from_block <= %s + 1 AND to_block >= %s - 1",
            (key, hi, lo),
        )
    cur.execute("INSERT INTO token_coverage (token, from_block, to_block) VALUES (%s, %s, %s)", (key, lo, hi))


def coverage_from_creation(cur, tokens) -> dict[str, int]:
    """The block each token is covered through, for tokens whose coverage reaches back to their registration."""
    addrs = sorted({_lower(token) for token in tokens if token})
    if not addrs:
        return {}
    cur.execute(
        "SELECT token, MAX(to_block) FROM ledger_token_coverage WHERE from_creation AND token = ANY(%s) GROUP BY token",
        (addrs,),
    )
    return {token: int(to_block) for token, to_block in cur.fetchall()}


def purge_wallets(cur, wallets) -> list[str]:
    """Remove everything an address earned while it was mistaken for a wallet; returns the tokens affected."""
    addrs = sorted({_lower(wallet) for wallet in wallets if wallet})
    if not addrs:
        return []
    cur.execute("DELETE FROM positions_v2 WHERE wallet = ANY(%s)", (addrs,))
    cur.execute("DELETE FROM parked_entitlements WHERE wallet = ANY(%s)", (addrs,))
    cur.execute("DELETE FROM wallet_flows WHERE wallet = ANY(%s) RETURNING token", (addrs,))
    return sorted({token for (token,) in cur.fetchall()})


def get_tx_meta(cur, txhashes) -> dict[str, TxMeta]:
    hashes = sorted({_lower(txhash) for txhash in txhashes})
    if not hashes:
        return {}
    cur.execute(_SELECT_TX_META_SQL, (hashes,))
    out: dict[str, TxMeta] = {}
    for row in cur.fetchall():
        values = dict(zip(TX_META_COLUMNS, row))
        values["value"] = _wei(values["value"])
        out[values["txhash"]] = TxMeta(**values)
    return out


def put_tx_meta(cur, metas) -> None:
    by_hash = {}
    for meta in metas:
        txhash = _lower(meta.txhash)
        by_hash[txhash] = (
            txhash,
            meta.block_number,
            meta.tx_index,
            _lower(meta.from_addr),
            _lower(meta.to_addr),
            _wei(meta.value),
            _lower(meta.selector),
        )
    if not by_hash:
        return
    execute_values(cur, _UPSERT_TX_META_SQL, list(by_hash.values()), page_size=_PAGE_SIZE)


def get_trace(cur, txhash: str) -> TraceResult | None:
    cur.execute("SELECT available, transfers FROM tx_traces WHERE txhash = %s", (_lower(txhash),))
    row = cur.fetchone()
    if row is None:
        return None
    transfers = [(str(src), str(dst), int(value)) for src, dst, value in (row[1] or [])]
    return TraceResult(available=bool(row[0]), transfers=transfers)


def put_trace(cur, txhash: str, result: TraceResult) -> None:
    transfers = [[_lower(src), _lower(dst), int(value)] for src, dst, value in (result.transfers or [])]
    cur.execute(
        """
        INSERT INTO tx_traces (txhash, available, transfers) VALUES (%s, %s, %s)
        ON CONFLICT (txhash) DO UPDATE SET available = EXCLUDED.available, transfers = EXCLUDED.transfers
        """,
        (_lower(txhash), bool(result.available), Json(transfers)),
    )


def get_kinds(cur, addrs) -> dict[str, str]:
    addresses = sorted({_lower(addr) for addr in addrs})
    if not addresses:
        return {}
    cur.execute("SELECT address, kind FROM address_kinds WHERE address = ANY(%s)", (addresses,))
    return {address: kind for address, kind in cur.fetchall()}


def put_kind(cur, addr: str, kind: str, source: str, block, evidence) -> None:
    cur.execute(
        """
        INSERT INTO address_kinds (address, kind, source, first_seen_block, evidence) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (address) DO UPDATE SET
            kind = EXCLUDED.kind,
            source = EXCLUDED.source,
            first_seen_block = LEAST(address_kinds.first_seen_block, EXCLUDED.first_seen_block),
            evidence = COALESCE(EXCLUDED.evidence, address_kinds.evidence)
        """,
        (_lower(addr), kind, source, block, Json(evidence) if evidence is not None else None),
    )


def upsert_venue(cur, addr: str, kind: str, token0=None, token1=None, discovered=False, evidence=None) -> None:
    cur.execute(
        """
        INSERT INTO venues (address, kind, token0, token1, discovered, evidence) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (address) DO UPDATE SET
            kind = EXCLUDED.kind,
            token0 = COALESCE(EXCLUDED.token0, venues.token0),
            token1 = COALESCE(EXCLUDED.token1, venues.token1),
            discovered = venues.discovered AND EXCLUDED.discovered,
            evidence = COALESCE(EXCLUDED.evidence, venues.evidence)
        """,
        (
            _lower(addr),
            kind,
            _lower(token0),
            _lower(token1),
            bool(discovered),
            Json(evidence) if evidence is not None else None,
        ),
    )


def _token_reg(row) -> TokenReg:
    return TokenReg(**dict(zip(REGISTRY_COLUMNS, row)))


def registry(cur, refresh: bool = False) -> dict[str, TokenReg]:
    global _REGISTRY
    if _REGISTRY is None or refresh:
        cur.execute(_SELECT_REGISTRY_SQL)
        _REGISTRY = {row[0]: _token_reg(row) for row in cur.fetchall()}
    return _REGISTRY


def refresh_registry(cur) -> dict[str, TokenReg]:
    return registry(cur, refresh=True)


def invalidate_registry() -> None:
    global _REGISTRY
    _REGISTRY = None


def register_token(cur, token: str, source: str, block, quote_token, decimals) -> TokenReg:
    cur.execute(_UPSERT_REGISTRY_SQL, (_lower(token), source, block, _lower(quote_token), decimals))
    reg = _token_reg(cur.fetchone())
    if _REGISTRY is not None:
        _REGISTRY[reg.token] = reg
    return reg


def get_ledger_meta(cur, key: str) -> str | None:
    cur.execute("SELECT value FROM ledger_meta WHERE key = %s", (key,))
    row = cur.fetchone()
    return None if row is None else row[0]


def set_ledger_meta(cur, key: str, value) -> None:
    cur.execute(
        "INSERT INTO ledger_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, None if value is None else str(value)),
    )
