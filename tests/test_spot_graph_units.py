import os
import sys
from decimal import Decimal

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.api  # noqa: F401,E402
import api.spot_graph as spot_graph  # noqa: E402
from core.multicall import abi_u256  # noqa: E402

WALLET = "0x" + "ab" * 20


class _Reply:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _aggregate3_result(returns: list[bytes]) -> str:
    """multicall3's (bool success, bytes returnData)[] answer, abi encoded."""
    elems = [abi_u256(1) + abi_u256(0x40) + abi_u256(len(d)) + d + bytes(-len(d) % 32) for d in returns]
    offsets, pos = [], 32 * len(elems)
    for e in elems:
        offsets.append(abi_u256(pos))
        pos += len(e)
    return "0x" + (abi_u256(0x20) + abi_u256(len(elems)) + b"".join(offsets) + b"".join(elems)).hex()


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


def test_lp_value_uses_historical_pool_tvl(monkeypatch):
    pool = "0x1111111111111111111111111111111111111111"
    cursor = _Cursor([(pool, Decimal(400))])
    monkeypatch.setattr(spot_graph, "db_cursor", lambda: cursor)
    balances = {
        f"{spot_graph._LP_BALANCE_PREFIX}{pool}": 25,
        f"{spot_graph._LP_SUPPLY_PREFIX}{pool}": 100,
    }

    value = spot_graph._lp_value_at(1234, balances)

    assert value == Decimal(100)
    assert cursor.calls[0][1] == ([pool], 1234)


def test_bucket_combines_wallet_vault_and_lp_values(monkeypatch):
    captured = {}
    monkeypatch.setattr(spot_graph, "_mon_usd_at", lambda ts: Decimal(2))
    monkeypatch.setattr(spot_graph, "_vault_value_at", lambda wallet, ts: Decimal(3))
    monkeypatch.setattr(spot_graph, "_lp_value_at", lambda ts, balances: Decimal(5))
    monkeypatch.setattr(
        spot_graph.storage,
        "write_spot_graph_bucket",
        lambda wallet, ts, block, usd, native, balances: captured.update(
            wallet=wallet,
            ts=ts,
            block=block,
            usd=usd,
            native=native,
            balances=balances,
        ),
    )

    spot_graph._write_bucket(
        "0x2222222222222222222222222222222222222222",
        1234,
        55,
        {"native": 10**18},
        [{"address": "native", "ticker": "MON", "decimals": 18}],
    )

    assert captured["usd"] == Decimal(10)
    assert captured["native"] == Decimal(5)
    assert captured["balances"]["__valueVersion"] == spot_graph.VALUE_VERSION


def test_a_dead_archive_node_hands_the_balance_batch_to_the_next_node(monkeypatch):
    """The archive key can die on the provider's side, as it did on 2026-09-10 when its allowlist began
    rejecting every server call; the graph then stopped at its last bucket instead of keeping the depth the
    other nodes reach."""
    archive = "https://archive.test/v2/key"
    public = "https://public.test"
    monkeypatch.setenv("SPOT_GRAPH_RPC", archive)
    monkeypatch.setenv("RPC_HTTP", public)
    monkeypatch.setenv("RPC_HTTP_FALLBACKS", "")
    monkeypatch.setattr(spot_graph, "_endpoints", None)
    asked = []

    def post(url, json=None, timeout=None):
        asked.append(url)
        if url == archive:
            raise httpx.ConnectError("refused", request=None)
        return _Reply([{"id": call["id"], "result": _aggregate3_result([abi_u256(7)])} for call in json])

    monkeypatch.setattr(spot_graph.rpc.httpx, "post", post)

    out = spot_graph._balances_at_many(WALLET, [(1000, 5)], [{"address": "native"}], [])

    assert asked == [archive, public]
    assert out == {1000: {"native": 7}}


def test_a_bucket_written_below_the_floor_lowers_it(monkeypatch):
    """The floor records the depth no node could reach. A fill that later writes below it, once the archive
    node is back, has to lower it, or complete stays true while history is still missing."""
    meta = {"spot_graph_floor": "2000"}
    monkeypatch.setattr(spot_graph.storage, "get_meta", lambda key: meta.get(key))
    monkeypatch.setattr(spot_graph.storage, "set_meta", lambda key, value: meta.__setitem__(key, value))
    monkeypatch.setattr(spot_graph.storage, "wallet_has_crystal_activity", lambda wallet: True)
    monkeypatch.setattr(spot_graph.storage, "list_lp_markets_for_graph", lambda wallet: [])
    monkeypatch.setattr(spot_graph.storage, "orders_for_graph", lambda wallet: [])
    monkeypatch.setattr(spot_graph.storage, "get_spot_graph_bucket_set", lambda wallet, since, version: set())
    monkeypatch.setattr(
        "api.spot_data.spot_token_list", lambda: [{"address": "native", "ticker": "MON", "decimals": 18}]
    )
    monkeypatch.setattr(spot_graph, "_wanted_buckets", lambda now: [3000, 2000, 1000])
    monkeypatch.setattr(spot_graph, "_block_at_ts", lambda ts: ts // 10)
    monkeypatch.setattr(
        spot_graph, "_balances_at_many", lambda wallet, pairs, tokens, lp: {ts: {"native": 1} for ts, _b in pairs}
    )
    written = []
    monkeypatch.setattr(
        spot_graph, "_write_bucket", lambda wallet, ts, block, balances, tokens, orders=None: written.append(ts)
    )
    monkeypatch.setattr(spot_graph.time, "sleep", lambda seconds: None)

    spot_graph._fill(WALLET)

    assert written == [3000, 2000, 1000]
    assert meta["spot_graph_floor"] == "1000"
