from decimal import Decimal

import api.spot_graph as spot_graph


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
