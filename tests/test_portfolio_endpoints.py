"""portfolio rest surface against a real database.

covers the fields the frontend previously re-derived client side: per row pnl and
last price on /user, the merged multi wallet batch, per day realized pnl, and usd
volume. the failure mode that matters is a number that disagrees with the position
columns the indexer maintains.
"""

import os
import sys
import time
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

import api.api  # noqa: E402, F401
from tests.test_launchpad_integration import (  # noqa: E402
    TOKEN,
    USER,
    _create,
    _lt_data,
    _new_state,
    _reserve_for,
    _router,
    _ta,
    _trade,
    clean,  # noqa: F401
    db,  # noqa: F401
)

OTHER = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def _trade_as(st, wallet, token=TOKEN, native_reserve=1500 * 10**18, blk=101, ts=1001, txh="0xbb", log_idx=0):
    from modules import launchpad as lp_mod

    ev = lp_mod.parse_launchpad_trade(
        _router(),
        ["0x", _ta(token), _ta(wallet)],
        _lt_data(True, 10**18, 10**20, native_reserve, _reserve_for(native_reserve)),
    )
    st.apply_launchpad_trade(ev, blk, ts, txh, log_idx, _router())
    return ev


def _today_ts(hour_offset_secs: int) -> int:
    now = int(time.time())
    midnight = now - (now % 86400)
    return midnight + hour_offset_secs


def test_user_rows_carry_pnl_and_last_price(db):
    from api.routes.launchpad import user_portfolio

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xr1", log_idx=0)

    body = user_portfolio(USER)
    assert body["positions"], "the trade must produce a position"
    row = body["positions"][0]
    for field in (
        "realized_pnl_native",
        "unrealized_pnl_native",
        "total_pnl_native",
        "last_price_native",
        "balance_native",
    ):
        assert row.get(field) is not None, f"missing {field}"
    assert Decimal(row["last_price_native"]) > 0

    summary = body["summary"]
    assert int(summary["native_spent"]) > 0
    assert summary["trade_count"] == 1
    assert "total_pnl_native" in summary and "portfolio_value_native" in summary


def test_merged_batch_sums_across_wallets(db):
    from api.routes.launchpad import user_portfolio, users_portfolio_batch

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xm1", log_idx=0)
    _trade_as(st, OTHER, native_reserve=1200 * 10**18, blk=102, ts=1002, txh="0xm2", log_idx=0)

    solo_a = user_portfolio(USER)["positions"][0]
    solo_b = user_portfolio(OTHER)["positions"][0]

    body = users_portfolio_batch(addresses=f"{USER},{OTHER}", token="", merged=True)
    merged = body["merged"]
    assert len(merged["positions"]) == 1, "one token means one merged row"
    row = merged["positions"][0]

    assert int(row["balance_token"]) == int(solo_a["balance_token"]) + int(solo_b["balance_token"])
    assert int(row["native_spent"]) == int(solo_a["native_spent"]) + int(solo_b["native_spent"])
    assert int(row["token_bought"]) == int(solo_a["token_bought"]) + int(solo_b["token_bought"])
    assert row["wallet_count"] == 2

    summary = merged["summary"]
    assert int(summary["native_spent"]) == int(solo_a["native_spent"]) + int(solo_b["native_spent"])
    assert summary["trade_count"] == 2
    assert summary["tokens_traded"] == 1


def test_unmerged_batch_shape_unchanged(db):
    from api.routes.launchpad import users_portfolio_batch

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xu1", log_idx=0)

    body = users_portfolio_batch(addresses=USER, token="")
    assert "users" in body and USER in body["users"]
    assert "merged" not in body


def _ledger_flow(**overrides):
    from core.ledger.types import Flow

    values = {
        "block_number": 101,
        "tx_index": 1,
        "log_index": 2,
        "sub_index": 0,
        "txhash": "0x" + "d1" * 32,
        "timestamp": _today_ts(3600),
        "wallet": USER,
        "token": TOKEN,
        "token_delta": 100 * 10**18,
        "quote_asset": "native",
        "quote_delta": -(10**18),
        "mon_value": Decimal(1),
        "usd_value": Decimal(2),
        "kind": "buy",
        "venue": _router(),
        "counterparty": _router(),
        "origin": USER,
        "source": "venue_event",
        "basis_state": "observed",
        "price_native": Decimal("0.01"),
        "basis_delta": 0,
        "realized_delta": 0,
    }
    values.update(overrides)
    return Flow(**values)


def test_daily_pnl_matches_position_realized(db):
    """The graph and the headline are one source: the day's realized is a slice of the fold's realized."""
    from api.routes.launchpad import portfolio_daily, user_portfolio
    from core.ledger import store
    from core.ledger.fold import fold_token
    from core.ledger.schema import init_ledger_schema
    from core.storage import db_cursor

    st = _new_state()
    _create(st, blk=100, ts=_today_ts(3500))
    with db_cursor() as cur:
        init_ledger_schema(cur)
        cur.execute("DELETE FROM wallet_flows WHERE wallet = %s", (USER,))
        cur.execute("DELETE FROM positions_v2 WHERE wallet = %s", (USER,))
        cur.execute("DELETE FROM token_fold_state WHERE token = %s", (TOKEN,))
        cur.execute("DELETE FROM launchpad_positions WHERE user_address = %s", (USER,))
        flows = [
            _ledger_flow(),
            _ledger_flow(
                block_number=102,
                txhash="0x" + "d2" * 32,
                timestamp=_today_ts(7200),
                token_delta=-(50 * 10**18),
                quote_delta=8 * 10**17,
                mon_value=Decimal("0.8"),
                usd_value=Decimal("1.6"),
                kind="sell",
            ),
        ]
        assert store.insert_flows(cur, flows) == 2
        assert store.refold_tokens(cur, {TOKEN: 0}, fold_token) == 1

    pos = user_portfolio(USER)["positions"][0]
    body = portfolio_daily(USER, days=7)
    assert len(body["rows"]) == 1, "both trades landed on one utc day"
    day = body["rows"][0]

    assert Decimal(day["realized_pnl_native"]) == Decimal(pos["realized_pnl_native"])
    assert Decimal(pos["realized_pnl_native"]) == Decimal(3 * 10**17)
    assert day["trade_count"] == 2
    assert day["buy_count"] == 1
    assert day["sell_count"] == 1
    assert int(day["buy_volume_native"]) == 10**18
    assert int(day["sell_volume_native"]) == 8 * 10**17
    assert body["as_of_block"] >= 0


def test_volume_reports_usd(db):
    from api.routes.launchpad import user_volume

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xv1", log_idx=0)

    body = user_volume(USER)
    assert "volume_usd" in body
    assert Decimal(body["volume_usd"]) >= 0
    assert int(Decimal(body["volume_native"])) > 0


def test_spot_body_shared_serializer(db, monkeypatch):
    import api.spot_data as spot_data

    monkeypatch.setattr(spot_data, "wallet_is_supported", lambda w: True)
    monkeypatch.setattr(spot_data, "fetch_balances", lambda w, t: (777, {}, 3 * 10**18, False))

    body = spot_data.spot_body("0x" + "cd" * 20)
    assert body["supported"] is True
    assert body["balance_block"] == 777
    rows = {r["address"]: r for r in body["rows"]}
    assert "native" in rows and rows["native"]["balanceRaw"] == str(3 * 10**18)
    assert "graph" not in body, "the graph belongs to rest, not the shared body"

    monkeypatch.setattr(spot_data, "wallet_is_supported", lambda w: False)
    empty = spot_data.spot_body("0x" + "cd" * 20)
    assert empty["supported"] is False and empty["rows"] == []


def test_spot_unsupported_wallet_is_flagged_and_costs_nothing(db, monkeypatch):
    import api.spot_data as spot_data
    import api.spot_graph as spot_graph
    from api.routes.launchpad import spot_portfolio

    spot_data._known_wallets.clear()
    spot_data._unknown_checked.clear()

    def boom(*a, **k):
        raise AssertionError("rpc must not be touched for an unsupported wallet")

    monkeypatch.setattr(spot_data, "fetch_balances", boom)
    monkeypatch.setattr(spot_graph, "ensure_fill", boom)

    body = spot_portfolio("0x00000000000000000000000000000000000000aa")
    assert body["supported"] is False
    assert body["rows"] == []
    assert body["graph"]["points"] == [] and body["graph"]["complete"] is True
    assert body["summary"]["totalAccountValue"] is None


def test_spot_supported_after_first_trade(db, monkeypatch):
    import api.spot_data as spot_data
    import api.spot_graph as spot_graph
    from api.routes.launchpad import spot_portfolio

    spot_data._known_wallets.clear()
    spot_data._unknown_checked.clear()

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xsp1", log_idx=0)

    monkeypatch.setattr(spot_data, "fetch_balances", lambda w, t: (12345, {}, 5 * 10**18, False))
    monkeypatch.setattr(spot_graph, "ensure_fill", lambda w: None)

    body = spot_portfolio(USER)
    assert body["supported"] is True
    assert body["balance_block"] == 12345


def test_user_include_native_balance(db, monkeypatch):
    import api.spot_data as spot_data
    from api.routes.launchpad import user_portfolio

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xn1", log_idx=0)

    monkeypatch.setattr(spot_data, "fetch_native_balance", lambda w: (123456, 7 * 10**18, False))
    body = user_portfolio(USER, include_native=True)
    assert body["native_balance"] == str(7 * 10**18)
    assert body["native_balance_block"] == 123456
    assert body["native_stale"] is False

    plain = user_portfolio(USER)
    assert "native_balance" not in plain, "the flag must stay opt in"


def test_unrealized_pnl_subtracts_cost_basis(db):
    import core.storage as storage

    wallet = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
    token = "0x350035555e10d9afaf1566aaebfced5ba6c27777"
    tokens_held = 4210753505605066288927
    spent = 290589750000000000000
    price = Decimal("0.068578")

    storage.upsert_position(
        user_address=wallet,
        token=token,
        token_bought_delta=tokens_held,
        token_sold_delta=0,
        native_spent_delta=spent,
        native_received_delta=0,
        balance_token_delta=tokens_held,
        realized_pnl_delta=0,
        trade_count_delta=1,
        buy_count_delta=1,
        sell_count_delta=0,
        last_price_native=price,
        cost_basis_delta=spent,
    )

    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT unrealized_pnl_native, total_pnl_native, cost_basis_native
            FROM launchpad_positions WHERE user_address=%s AND token=%s
            """,
            (wallet, token),
        )
        unrealized, total, cost_basis = cur.fetchone()

    expected = Decimal(tokens_held) * price - Decimal(spent)
    assert abs(unrealized - expected) < Decimal("1e-6"), "unrealized is value minus cost, not value"
    assert unrealized < 0, "a position worth slightly less than it cost is a loss"
    assert abs(unrealized) < Decimal(spent) / 10, "the loss is small, not the size of the position"
    assert total == unrealized, "nothing was sold, so total pnl is the unrealized part"
    assert cost_basis == Decimal(spent)


def test_pool_reserves_follow_token_ordering(db):
    import core.storage as storage

    token = "0x350035555e10d9afaf1566aaebfced5ba6c27777"
    native = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
    with storage.db_cursor() as cur:
        cur.execute("DELETE FROM launchpad_pools WHERE pool IN ('0xpool0', '0xpool1')")
        cur.execute(
            "INSERT INTO launchpad_pools (pool, token_addr, native_addr, token_is_0) VALUES (%s,%s,%s,TRUE)",
            ("0xpool0", token, native),
        )
        cur.execute(
            "INSERT INTO launchpad_pools (pool, token_addr, native_addr, token_is_0) VALUES (%s,%s,%s,FALSE)",
            ("0xpool1", token, native),
        )

    storage.update_pool_reserves("0xpool0", 1000, 7, 100, 1000)
    storage.update_pool_reserves("0xpool1", 1000, 7, 100, 1000)

    with storage.db_cursor() as cur:
        cur.execute(
            "SELECT pool, reserve_token, reserve_native FROM launchpad_pools WHERE pool LIKE '0xpool%' ORDER BY pool"
        )
        got = {p: (int(rt), int(rn)) for p, rt, rn in cur.fetchall()}
    assert got["0xpool0"] == (1000, 7), "token is reserve0 here"
    assert got["0xpool1"] == (7, 1000), "token is reserve1 here, so the sides swap"

    storage.update_pool_reserves("0xpool0", 1, 1, 50, 500)
    with storage.db_cursor() as cur:
        cur.execute("SELECT reserve_token FROM launchpad_pools WHERE pool = '0xpool0'")
        assert int(cur.fetchone()[0]) == 1000, "a stale sync is ignored"


def test_spot_graph_ends_on_the_live_total_not_the_last_stored_bucket(db, monkeypatch):
    """Buckets are hourly and filled by a background thread after the response is built, so a first load
    used to draw an hour-old last point and the next refresh a fresh one. The live total the same response
    already carries is the last point."""
    import time

    import api.spot_data as spot_data
    import api.spot_graph as spot_graph
    from api.routes.launchpad import spot_portfolio

    wallet = "0x" + "ce" * 20
    stale = int(time.time()) - 7200

    def body(w, include_zero=False):
        return {
            "wallet": wallet,
            "wallets": [wallet],
            "supported": True,
            "rows": [],
            "vaults": [],
            "liquidity": [],
            "orders": [],
            "summary": {"totalAccountValue": "275.00000000", "walletValue": "200.00000000"},
            "balance_block": 5,
            "stale": False,
        }

    monkeypatch.setattr(spot_data, "spot_body", body)
    monkeypatch.setattr(spot_graph, "ensure_fill", lambda w: None)
    monkeypatch.setattr(
        spot_graph, "graph_for", lambda w: {"resolution": 3600, "points": [{"t": stale, "v": 230.0}], "complete": True}
    )

    out = spot_portfolio(wallet)
    points = out["graph"]["points"]
    assert points[0] == {"t": stale, "v": 230.0}
    assert points[-1]["v"] == 275.0
    assert points[-1]["t"] >= int(time.time()) - 5
    assert points[-1]["live"] is True


def test_a_fee_claim_in_the_activity_feed_carries_its_price_and_dollar_value(db):
    """A referral fee paid in WMON is worth one MON per MON and the dollars MON was worth when it landed;
    a claim paid in a launchpad token is priced at that token's last trade before the claim. Both used to
    come through as zero."""
    import core.storage as storage
    from core.storage import db_cursor

    WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xf1", log_idx=0)
    with db_cursor() as cur:
        cur.execute(
            "SELECT usd_amount / (native_amount / 1e18), price_native FROM launchpad_trades WHERE user_address = %s ORDER BY timestamp DESC LIMIT 1",
            (USER,),
        )
        mon_usd, token_price = (Decimal(str(v)) for v in cur.fetchone())
        assert mon_usd > 0 and token_price > 0
        cur.execute("DELETE FROM referral_claims WHERE user_address = %s", (USER,))
        cur.execute(
            "INSERT INTO referral_claims (txhash, log_index, claim_index, block_number, timestamp, user_address, token, amount) VALUES (%s, %s, %s, %s, %s, %s, %s, %s), (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "0xfeeclaim1",
                3,
                0,
                102,
                2000,
                USER,
                WMON,
                2 * 10**18,
                "0xfeeclaim2",
                4,
                0,
                102,
                2000,
                USER,
                TOKEN,
                5 * 10**18,
            ),
        )

    items = {i["txhash"]: i for i in storage.wallet_activity([USER], limit=50) if i["type"] == "fee_claim"}
    wmon = items["0xfeeclaim1"]
    assert wmon["amountNative"] == str(2 * 10**18)
    assert Decimal(wmon["priceNative"]) == Decimal(1)
    assert Decimal(wmon["usdAmount"]) == (Decimal(2) * mon_usd).quantize(Decimal("1e-18"))
    token = items["0xfeeclaim2"]
    assert token["amountToken"] == str(5 * 10**18)
    assert Decimal(token["priceNative"]) == token_price
    assert Decimal(token["usdAmount"]) == (Decimal(5) * token_price * mon_usd).quantize(Decimal("1e-18"))
