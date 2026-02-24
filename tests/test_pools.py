import os
import os
import sys
import types
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.routes.pools as pool_api
import core.storage.pools as pool_storage
from fastapi import HTTPException
from modules import pools


def _u256(n: int) -> str:
    return f"{int(n):064x}"


def _topic_addr(addr: str) -> str:
    h = addr.lower().replace("0x", "")
    return "0x" + ("0" * 24) + h


class _SqlCur:
    def __init__(self, *, fetchone_result=None, fetchall_result=None):
        self._fetchone_result = fetchone_result
        self._fetchall_result = [] if fetchall_result is None else fetchall_result
        self.exec_calls = []

    def execute(self, sql, params=None):
        self.exec_calls.append((sql, params))

    def fetchone(self):
        return self._fetchone_result

    def fetchall(self):
        if isinstance(self._fetchall_result, list):
            return list(self._fetchall_result)
        return self._fetchall_result


@contextmanager
def _yield_cursor(cur):
    yield cur


def _assert_http_exc(fn, status: int):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == status
        return e
    raise AssertionError("expected HTTPException")


def _pool_row_25(updated_at=2000, last_sync_at=2100):
    return (
        "0xPool",
        "0xQuote",
        "0xBase",
        2,
        6,
        18,
        "USDC",
        "USD Coin",
        "WMON",
        "Wrapped MON",
        300,
        True,
        Decimal("1.23"),
        updated_at,
        1000,
        123456,
        654321,
        9999,
        Decimal("777.7"),
        Decimal("88.8"),
        Decimal("9.9"),
        Decimal("0.12"),
        Decimal("0.003"),
        55,
        last_sync_at,
    )


def test_pools_module_helpers_and_parsers():
    assert pools.to_addr("abcdef") == "0xabcdef"
    assert pools.to_addr(bytes.fromhex("00" * 19 + "12")) == "0x" + ("00" * 19 + "12")
    assert list(pools._chunks("abcdef", 2)) == ["ab", "cd", "ef"]

    assert pools._u(["0a"], 0) == 10
    assert pools._u([], 9, default=7) == 7
    assert pools._u(["zz"], 0, default=8) == 8

    sync = pools.parse_sync(
        "0xrouter",
        ["0xtopic0", _topic_addr("0x1111111111111111111111111111111111111111")],
        _u256(100) + _u256(200),
    )
    assert sync == {
        "market": "0x1111111111111111111111111111111111111111",
        "reserveQuote": 100,
        "reserveBase": 200,
    }

    sync_fallback = pools.parse_sync("0xrouter", ["0xtopic0"], _u256(1))
    assert sync_fallback["market"] == "0x" + ("0" * 40)
    assert sync_fallback["reserveQuote"] == 1
    assert sync_fallback["reserveBase"] == 0

    mint = pools.parse_mint(
        "0xrouter",
        [
            "0xtopic0",
            _topic_addr("0x2222222222222222222222222222222222222222"),
            _topic_addr("0x3333333333333333333333333333333333333333"),
        ],
        _u256(11) + _u256(22),
    )
    assert mint["market"] == "0x2222222222222222222222222222222222222222"
    assert mint["sender"] == "0x3333333333333333333333333333333333333333"
    assert mint["amountQuote"] == 11
    assert mint["amountBase"] == 22

    mint_fallback = pools.parse_mint("0xrouter", ["0xtopic0"], "zz")
    assert mint_fallback["market"] == "0x" + ("0" * 40)
    assert mint_fallback["sender"] == "0x" + ("0" * 40)
    assert mint_fallback["amountQuote"] == 0
    assert mint_fallback["amountBase"] == 0

    burn = pools.parse_burn(
        "0xrouter",
        [
            "0xtopic0",
            _topic_addr("0x4444444444444444444444444444444444444444"),
            _topic_addr("0x5555555555555555555555555555555555555555"),
            _topic_addr("0x6666666666666666666666666666666666666666"),
        ],
        _u256(33) + _u256(44),
    )
    assert burn["market"] == "0x4444444444444444444444444444444444444444"
    assert burn["sender"] == "0x5555555555555555555555555555555555555555"
    assert burn["to"] == "0x6666666666666666666666666666666666666666"
    assert burn["amountQuote"] == 33
    assert burn["amountBase"] == 44

    burn_fallback = pools.parse_burn("0xrouter", ["0xtopic0"], "")
    assert burn_fallback["market"] == "0x" + ("0" * 40)
    assert burn_fallback["sender"] == "0x" + ("0" * 40)
    assert burn_fallback["to"] == "0x" + ("0" * 40)
    assert burn_fallback["amountQuote"] == 0
    assert burn_fallback["amountBase"] == 0


def test_pool_storage_read_helpers_and_query_builders():
    cur_load = _SqlCur(fetchall_result=[("m",)])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_load)):
        assert pool_storage.load_crystal_pool_states_for_state() == [("m",)]
    assert "FROM crystal_pools" in cur_load.exec_calls[0][0]

    cur_get = _SqlCur(fetchone_result=("row",))
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_get)):
        assert pool_storage.get_crystal_pool_state("0xPOOL") == ("row",)
    assert cur_get.exec_calls[0][1] == ("0xpool",)

    cur_get_direct = MagicMock()
    cur_get_direct.fetchone.return_value = ("direct",)
    assert pool_storage.get_crystal_pool_state("0xPOOL", cur=cur_get_direct) == ("direct",)
    assert cur_get_direct.execute.call_args[0][1] == ("0xpool",)

    cur_sum = _SqlCur(fetchone_result=(Decimal("12.3"), Decimal("4.5")))
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_sum)):
        assert pool_storage.sum_crystal_pool_trade_metrics_since("0xPOOL", 123) == (
            Decimal("12.3"),
            Decimal("4.5"),
        )
    assert "kind = 'trade'" in cur_sum.exec_calls[0][0]
    assert cur_sum.exec_calls[0][1] == ("0xpool", 123)

    cur_sum_direct = MagicMock()
    cur_sum_direct.fetchone.return_value = (1, 2)
    assert pool_storage.sum_crystal_pool_trade_metrics_since("0xPOOL", 0, cur=cur_sum_direct) == (1, 2)
    assert cur_sum_direct.execute.call_args[0][1] == ("0xpool", 0)

    cur_twa = _SqlCur(fetchone_result=(Decimal("42.5"),))
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_twa)):
        assert pool_storage.time_weighted_avg_crystal_pool_tvl_since("0xPOOL", 111, 222) == Decimal("42.5")
    twa_sql, twa_params = cur_twa.exec_calls[0]
    assert "LEAD(timestamp) OVER" in twa_sql
    assert "weighted_sum" in twa_sql
    assert twa_params == ("0xpool", 111, 222)

    cur_twa_direct = MagicMock()
    cur_twa_direct.fetchone.return_value = (Decimal("9.9"),)
    assert pool_storage.time_weighted_avg_crystal_pool_tvl_since("0xPOOL", 1, 2, cur=cur_twa_direct) == Decimal("9.9")
    assert cur_twa_direct.execute.call_args[0][1] == ("0xpool", 1, 2)

    cur_twa_empty = _SqlCur(fetchone_result=None)
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_twa_empty)):
        assert pool_storage.time_weighted_avg_crystal_pool_tvl_since("0xPOOL", 0, 0) == 0

    cur_samples_a = _SqlCur(fetchall_result=[(3, Decimal("3.0")), (2, Decimal("2.0"))])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_samples_a)):
        rows_a = pool_storage.list_crystal_pool_tvl_samples("0xPOOL", since_ts=None, limit=-1)
    sql_a, params_a = cur_samples_a.exec_calls[0]
    assert "WHERE market = %s" in sql_a
    assert "timestamp >= %s" not in sql_a
    assert "LIMIT %s" in sql_a
    assert params_a == ("0xpool", 1)
    assert rows_a == [(3, Decimal("3.0")), (2, Decimal("2.0"))]

    cur_samples_b = _SqlCur(fetchall_result=[(1, Decimal("1.0"))])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_samples_b)):
        rows_b = pool_storage.list_crystal_pool_tvl_samples("0xPOOL", since_ts=999, limit=999999)
    sql_b, params_b = cur_samples_b.exec_calls[0]
    assert "timestamp >= %s" in sql_b
    assert params_b == ("0xpool", 999, 5000)
    assert rows_b == [(1, Decimal("1.0"))]

    cur_list = _SqlCur(fetchall_result=[("pool",)])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_list)):
        assert pool_storage.list_crystal_pools_with_state() == [("pool",)]
    assert "FROM crystal_markets cm" in cur_list.exec_calls[0][0]
    assert "AND cm.is_amm_enabled = TRUE" in cur_list.exec_calls[0][0]
    assert "COALESCE(cp.volume_24h_usd, 0) DESC" in cur_list.exec_calls[0][0]
    assert cur_list.exec_calls[0][1] == (50, 0)

    cur_list_filtered = _SqlCur(fetchall_result=[("pool",)])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_list_filtered)):
        pool_storage.list_crystal_pools_with_state(
            search="mon/usdc",
            page=2,
            limit=999,
            sort_by="apy",
            sort_dir="asc",
        )
    sql_list_f, params_list_f = cur_list_filtered.exec_calls[0]
    assert "LOWER(cm.market) LIKE %s" in sql_list_f
    assert "COALESCE(cp.apy_24h, 0) ASC" in sql_list_f
    assert params_list_f == ("%mon/usdc%",) * 7 + (50, 50)

    cur_list_tok1 = _SqlCur(fetchall_result=[("pool",)])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_list_tok1)):
        pool_storage.list_crystal_pools_with_state(token_addresses=["0xAAA"])
    sql_list_t1, params_list_t1 = cur_list_tok1.exec_calls[0]
    assert "LOWER(cm.quote_address) = %s" in sql_list_t1
    assert "LOWER(cm.base_address) = %s" in sql_list_t1
    assert params_list_t1 == ("0xaaa", "0xaaa", 50, 0)

    cur_list_tok2 = _SqlCur(fetchall_result=[("pool",)])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_list_tok2)):
        pool_storage.list_crystal_pools_with_state(token_addresses=["0xAAA", "0xBBB", "0xCCC"])
    sql_list_t2, params_list_t2 = cur_list_tok2.exec_calls[0]
    assert "(LOWER(cm.quote_address) = %s AND LOWER(cm.base_address) = %s)" in sql_list_t2
    assert params_list_t2 == ("0xaaa", "0xbbb", "0xbbb", "0xaaa", 50, 0)

    cur_list_bad_sort = _SqlCur(fetchall_result=[("pool",)])
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_list_bad_sort)):
        pool_storage.list_crystal_pools_with_state(sort_by="zzz", sort_dir="sideways")
    sql_bad_sort, _ = cur_list_bad_sort.exec_calls[0]
    assert "COALESCE(cp.volume_24h_usd, 0) DESC" in sql_bad_sort

    cur_one = _SqlCur(fetchone_result=("pool1",))
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_one)):
        assert pool_storage.get_crystal_pool_with_state("0xPOOL") == ("pool1",)
    sql_one, params_one = cur_one.exec_calls[0]
    assert "AND cm.market = %s" in sql_one
    assert params_one == ("0xpool",)


def test_pool_storage_write_helpers_sql_and_cursor_paths():
    up_none = _SqlCur()
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(up_none)):
        pool_storage.upsert_crystal_pool_state(
            market="0xPOOL",
            reserve_quote=10,
            reserve_base=20,
            total_shares=None,
            tvl_usd="1.25",
            volume_24h_usd="2.50",
            fees_24h_usd="0.50",
            apy_24h="0.10",
            daily_yield_24h="0.01",
            last_sync_block=None,
            last_sync_at=None,
            updated_block=None,
            updated_at=None,
        )
    (sql1, params1), = up_none.exec_calls
    assert "INSERT INTO crystal_pools" in sql1 and "ON CONFLICT (market)" in sql1
    assert params1[0] == "0xpool"
    assert params1[1:4] == (10, 20, None)
    assert params1[4:9] == (
        Decimal("1.25"),
        Decimal("2.50"),
        Decimal("0.50"),
        Decimal("0.10"),
        Decimal("0.01"),
    )
    assert params1[9:13] == (None, None, None, None)
    assert params1[13] is None

    up_direct = MagicMock()
    pool_storage.upsert_crystal_pool_state(
        market="0xPOOL",
        reserve_quote=1,
        reserve_base=2,
        total_shares=3,
        tvl_usd=0,
        volume_24h_usd=0,
        fees_24h_usd=0,
        apy_24h=0,
        daily_yield_24h=0,
        last_sync_block=4,
        last_sync_at=5,
        updated_block=6,
        updated_at=7,
        cur=up_direct,
    )
    _, params2 = up_direct.execute.call_args[0]
    assert params2[0] == "0xpool"
    assert params2[1:4] == (1, 2, 3)
    assert params2[9:13] == (4, 5, 6, 7)
    assert params2[13] == 3

    uts_none = _SqlCur()
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(uts_none)):
        pool_storage.update_crystal_pool_total_shares("0xPOOL", 99, updated_block=None, updated_at=None)
    (uts_sql, uts_params), = uts_none.exec_calls
    assert "INSERT INTO crystal_pools (market, total_shares, updated_block, updated_at)" in uts_sql
    assert uts_params == ("0xpool", 99, None, None)

    uts_direct = MagicMock()
    pool_storage.update_crystal_pool_total_shares("0xPOOL", 100, 7, 8, cur=uts_direct)
    _, uts_params2 = uts_direct.execute.call_args[0]
    assert uts_params2 == ("0xpool", 100, 7, 8)

    sync_none = _SqlCur()
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(sync_none)):
        pool_storage.insert_crystal_pool_sync_event(
            block_number=1,
            log_index=2,
            timestamp=3,
            market="0xPOOL",
            txhash="0xHASH",
            kind="trade",
            reserve_quote=4,
            reserve_base=5,
            prev_reserve_quote=None,
            prev_reserve_base=6,
            volume_usd="7.7",
            fees_usd="0.7",
        )
    (sync_sql, sync_params), = sync_none.exec_calls
    assert "INSERT INTO crystal_pool_sync_events" in sync_sql
    assert sync_params[:10] == (1, 2, 3, "0xpool", "0xhash", "trade", 4, 5, None, 6)
    assert sync_params[10] == Decimal("7.7")
    assert sync_params[11] == Decimal("0.7")

    sync_direct = MagicMock()
    pool_storage.insert_crystal_pool_sync_event(
        block_number=0,
        log_index=0,
        timestamp=0,
        market="0xPOOL",
        txhash="0xHASH",
        kind="other",
        reserve_quote=0,
        reserve_base=0,
        prev_reserve_quote=0,
        prev_reserve_base=0,
        volume_usd=None,
        fees_usd=None,
        cur=sync_direct,
    )
    assert sync_direct.execute.call_count == 1

    tvl_none = _SqlCur()
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(tvl_none)):
        pool_storage.insert_crystal_pool_tvl_sample(
            market="0xPOOL",
            block_number=11,
            log_index=12,
            timestamp=13,
            reserve_quote=14,
            reserve_base=15,
            tvl_usd="16.5",
            txhash="0xTX",
        )
    (tvl_sql, tvl_params), = tvl_none.exec_calls
    assert "INSERT INTO crystal_pool_tvl_samples" in tvl_sql
    assert tvl_params[:7] == ("0xpool", 11, 12, 13, 14, 15, Decimal("16.5"))
    assert tvl_params[7] == "0xtx"

    tvl_direct = MagicMock()
    pool_storage.insert_crystal_pool_tvl_sample(
        market="0xPOOL",
        block_number=0,
        log_index=0,
        timestamp=0,
        reserve_quote=0,
        reserve_base=0,
        tvl_usd=None,
        txhash="0xTX",
        cur=tvl_direct,
    )
    assert tvl_direct.execute.call_count == 1

    lp_none = _SqlCur()
    with patch.object(pool_storage, "db_cursor", side_effect=lambda: _yield_cursor(lp_none)):
        pool_storage.upsert_crystal_pool_lp_user_delta(
            market="0xPOOL",
            user_address="0xUSER",
            shares_delta=-5,
            last_transfer=None,
            updated_block=44,
        )
    (lp_sql, lp_params), = lp_none.exec_calls
    assert "INSERT INTO crystal_pool_lp_users" in lp_sql
    assert lp_params == ("0xpool", "0xuser", -5, None, 44, -5, None, 44)

    lp_direct = MagicMock()
    pool_storage.upsert_crystal_pool_lp_user_delta(
        market="0xPOOL",
        user_address="0xUSER",
        shares_delta=6,
        last_transfer=77,
        updated_block=None,
        cur=lp_direct,
    )
    _, lp_params2 = lp_direct.execute.call_args[0]
    assert lp_params2 == ("0xpool", "0xuser", 6, 77, None, 6, 77, None)


def test_pools_route_helper_and_list_endpoint():
    fake_api = types.SimpleNamespace(_crystal_pool_row_to_api=MagicMock(return_value={"ok": True}))
    with patch.dict(sys.modules, {"api.api": fake_api}):
        assert pool_api._pool_row_to_api(("row",)) == {"ok": True}
    fake_api._crystal_pool_row_to_api.assert_called_once_with(("row",))

    rows = [tuple(["a"] * 25 + [2]), tuple(["b"] * 25 + [2])]
    with patch.object(pool_api.storage, "list_crystal_pools_with_state", return_value=rows) as list_store, patch.object(
        pool_api, "_pool_row_to_api", side_effect=[{"id": "a"}, {"id": "b"}]
    ) as helper:
        out = pool_api.list_pools()
    list_store.assert_called_once_with(search="", token_addresses=[], page=1, limit=50, sort_by="volume", sort_dir="desc")
    assert out["ok"] is True
    assert out["pools"] == [{"id": "a"}, {"id": "b"}]
    assert out["count"] == 2 and out["total"] == 2 and out["page"] == 1 and out["limit"] == 50
    assert out["hasMore"] is False and out["sort"] == "volume" and out["order"] == "desc" and out["search"] == ""
    assert out["tokens"] == []
    assert helper.call_count == 2

    with patch.object(pool_api.storage, "list_crystal_pools_with_state", return_value=[]):
        _assert_http_exc(lambda: pool_api.list_pools(sort="bad"), 400)
    with patch.object(pool_api.storage, "list_crystal_pools_with_state", return_value=[]):
        _assert_http_exc(lambda: pool_api.list_pools(order="bad"), 400)

    with patch.object(pool_api.storage, "list_crystal_pools_with_state", return_value=[]) as list_store2:
        out2 = pool_api.list_pools(search="mon", tokens="0xAA,0xBB", sort="tvl", order="asc", page=3, limit=7)
    list_store2.assert_called_once_with(search="mon", token_addresses=["0xaa", "0xbb"], page=3, limit=7, sort_by="tvl", sort_dir="asc")
    assert out2["count"] == 0 and out2["total"] == 0 and out2["hasMore"] is False
    assert out2["tokens"] == ["0xaa", "0xbb"]

    with patch.object(pool_api.storage, "list_crystal_pools_with_state", return_value=[]) as list_store3:
        out3 = pool_api.list_pools(tokens="  , 0xAA ,, ")
    list_store3.assert_called_once_with(search="", token_addresses=["0xaa"], page=1, limit=50, sort_by="volume", sort_dir="desc")
    assert out3["tokens"] == ["0xaa"]


def test_pools_route_get_pool_success_and_history_building():
    row = _pool_row_25(updated_at=5000, last_sync_at=5100)
    api_row = {
        "market": "0xpool",
        "updatedAt": 5100,
        "apy24h": 0.12,
    }
    samples = [(4000, Decimal("10.5")), (5000, Decimal("11.0"))]
    with patch.object(pool_api.storage, "get_crystal_pool_with_state", return_value=row) as get_one, patch.object(
        pool_api, "_pool_row_to_api", return_value=dict(api_row)
    ) as helper, patch.object(
        pool_api.storage,
        "list_crystal_pool_tvl_samples",
        return_value=samples,
    ) as list_hist:
        out = pool_api.get_pool("0xPOOL", history_seconds=1000, history_limit=7)

    get_one.assert_called_once_with("0xPOOL")
    helper.assert_called_once_with(row)
    list_hist.assert_called_once_with("0xpool", since_ts=4100, limit=7)
    assert out["tvlHistory"] == [{"timestamp": 4000, "tvl": 10.5}, {"timestamp": 5000, "tvl": 11.0}]
    assert out["apyHistory"] == [{"timestamp": 4000, "apy": 0.12}, {"timestamp": 5000, "apy": 0.12}]


def test_pools_route_get_pool_fallback_params_no_since_and_not_found():
    row = _pool_row_25(updated_at=0, last_sync_at=0)
    with patch.object(pool_api.storage, "get_crystal_pool_with_state", return_value=row), patch.object(
        pool_api, "_pool_row_to_api", return_value={"market": "0xpool", "updatedAt": 0, "apy24h": 0}
    ), patch.object(
        pool_api.storage, "list_crystal_pool_tvl_samples", return_value=[]
    ) as list_hist:
        out = pool_api.get_pool("0xPOOL", history_seconds="bad", history_limit="bad")
    list_hist.assert_called_once_with("0xpool", since_ts=None, limit=500)
    assert out["tvlHistory"] == []
    assert out["apyHistory"] == []

    with patch.object(pool_api.storage, "get_crystal_pool_with_state", return_value=row), patch.object(
        pool_api, "_pool_row_to_api", return_value={"market": "0xpool", "updatedAt": 100, "apy24h": 0}
    ), patch.object(
        pool_api.storage, "list_crystal_pool_tvl_samples", return_value=[]
    ) as list_hist2:
        pool_api.get_pool("0xPOOL", history_seconds=0, history_limit=1)
    list_hist2.assert_called_once_with("0xpool", since_ts=None, limit=1)

    with patch.object(pool_api.storage, "get_crystal_pool_with_state", return_value=None):
        _assert_http_exc(lambda: pool_api.get_pool("0xMISSING"), 404)


if __name__ == "__main__":
    for fn in [
        test_pools_module_helpers_and_parsers,
        test_pool_storage_read_helpers_and_query_builders,
        test_pool_storage_write_helpers_sql_and_cursor_paths,
        test_pools_route_helper_and_list_endpoint,
        test_pools_route_get_pool_success_and_history_building,
        test_pools_route_get_pool_fallback_params_no_since_and_not_found,
    ]:
        fn()
    print("Pool tests passed")
