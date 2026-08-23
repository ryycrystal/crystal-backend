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

# api.api must load before any api.routes import, the route registration order is
# load bearing and importing a route module first trips the circular import
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


# rest rows must carry the pnl and price fields the socket already sends, or the
# client keeps re-deriving them four different ways
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


# the merged batch must equal the sum of the individual wallets, computed in one
# query rather than n responses stitched together in the browser
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


# an unmerged batch keeps its old shape so existing clients are untouched
def test_unmerged_batch_shape_unchanged(db):
    from api.routes.launchpad import users_portfolio_batch

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xu1", log_idx=0)

    body = users_portfolio_batch(addresses=USER, token="")
    assert "users" in body and USER in body["users"]
    assert "merged" not in body


# a full same day round trip's daily realized pnl must equal the realized pnl the
# position columns hold, because both are the same average cost formula
def test_daily_pnl_matches_position_realized(db):
    from api.routes.launchpad import portfolio_daily, user_portfolio
    from modules import launchpad as lp_mod

    buy_ts = _today_ts(3600)
    sell_ts = _today_ts(7200)

    st = _new_state()
    _create(st, blk=100, ts=buy_ts - 100)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=buy_ts, txh="0xd1", log_idx=0)

    sell_reserve = 1100 * 10**18 - 5 * 10**17
    sell = lp_mod.parse_launchpad_trade(
        _router(),
        ["0x", _ta(TOKEN), _ta(USER)],
        _lt_data(False, 10**20, 5 * 10**17, sell_reserve, _reserve_for(sell_reserve)),
    )
    st.apply_launchpad_trade(sell, 102, sell_ts, "0xd2", 0, _router())

    pos = user_portfolio(USER)["positions"][0]
    body = portfolio_daily(USER, days=7)
    assert len(body["rows"]) == 1, "both trades landed on one utc day"
    day = body["rows"][0]

    assert Decimal(day["realized_pnl_native"]) == Decimal(pos["realized_pnl_native"])
    assert day["trade_count"] == 2
    assert day["buy_count"] == 1
    assert day["sell_count"] == 1
    assert int(day["buy_volume_native"]) == 10**18
    assert int(day["sell_volume_native"]) == 5 * 10**17


# usd volume comes from what each trade actually printed, never today's price
def test_volume_reports_usd(db):
    from api.routes.launchpad import user_volume

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xv1", log_idx=0)

    body = user_volume(USER)
    assert "volume_usd" in body
    assert Decimal(body["volume_usd"]) >= 0
    assert int(Decimal(body["volume_native"])) > 0


# the shared spot body serves rest and the ws balances channel identically
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


# a wallet that never touched crystal is refused before any rpc spend
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


# one indexed trade makes the wallet supported and the endpoint serves normally
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


# include_native adds the wallet's mon balance without a client side rpc read
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


# unrealized profit is what a position is worth now minus what it cost. storing
# the market value instead made every untouched position report its whole size
# as profit, so a break even bag showed as a near total gain
def test_unrealized_pnl_subtracts_cost_basis(db):
    import core.storage as storage

    wallet = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
    token = "0x350035555e10d9afaf1566aaebfced5ba6c27777"
    # bought 4210.75 tokens for 290.59 native, nothing sold, price barely moved
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



# a pair reports reserve0/reserve1 positionally, so which side is the token
# depends on the address ordering recorded when the pool was discovered
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

    # reserve0=1000 tokens, reserve1=7 native
    storage.update_pool_reserves("0xpool0", 1000, 7, 100, 1000)
    storage.update_pool_reserves("0xpool1", 1000, 7, 100, 1000)

    with storage.db_cursor() as cur:
        cur.execute("SELECT pool, reserve_token, reserve_native FROM launchpad_pools WHERE pool LIKE '0xpool%' ORDER BY pool")
        got = {p: (int(rt), int(rn)) for p, rt, rn in cur.fetchall()}
    assert got["0xpool0"] == (1000, 7), "token is reserve0 here"
    assert got["0xpool1"] == (7, 1000), "token is reserve1 here, so the sides swap"

    # an older sync must not overwrite a newer one, the same monotonic rule the
    # orderbook appliers use
    storage.update_pool_reserves("0xpool0", 1, 1, 50, 500)
    with storage.db_cursor() as cur:
        cur.execute("SELECT reserve_token FROM launchpad_pools WHERE pool = '0xpool0'")
        assert int(cur.fetchone()[0]) == 1000, "a stale sync is ignored"
