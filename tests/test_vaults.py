import os
import sys
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

import api.routes.vaults as vault_api
import core.storage.vaults as vault_storage
from modules import markets, vaults


def _u256(n: int) -> bytes:
    return int(n).to_bytes(32, "big")


def _addr_word(addr: str) -> bytes:
    h = addr.lower().replace("0x", "")
    return (b"\x00" * 12) + bytes.fromhex(h)


def _topic_addr(addr: str) -> str:
    h = addr.lower().replace("0x", "")
    return "0x" + ("0" * 24) + h


def _abi_bytes(b: bytes) -> bytes:
    pad = (-len(b)) % 32
    return _u256(len(b)) + b + (b"\x00" * pad)


def _abi_str(s: str) -> bytes:
    return _abi_bytes(s.encode("utf-8"))


def _token_meta(token: str, decimals: int, ticker: str, name: str) -> bytes:
    ticker_enc = _abi_str(ticker)
    name_enc = _abi_str(name)
    head_len = 32 * 4
    ticker_off = head_len
    name_off = head_len + len(ticker_enc)
    return b"".join(
        [
            _addr_word(token),
            _u256(decimals),
            _u256(ticker_off),
            _u256(name_off),
            ticker_enc,
            name_enc,
        ]
    )


def _vault_meta_tuple(strings: list[str]) -> bytes:
    enc = [_abi_str(s) for s in strings]
    head_len = 32 * len(strings)
    offs = []
    cur = head_len
    for e in enc:
        offs.append(cur)
        cur += len(e)
    return b"".join([*[_u256(o) for o in offs], *enc])


def _market_created_data() -> tuple[list[str], str]:
    market = "0x8a34f54f0f5f2fedfe2beb1e46cfc7a25c0df99b"
    quote_asset = "0x754704bc059f8c67012fed69bc8a327a5aafb603"
    base_asset = "0x00000000efe302beaa2b3e6e1b18d08d69a9012a"
    quote_meta = _token_meta(quote_asset, 6, "USDC", "USD Coin")
    base_meta = _token_meta(base_asset, 18, "WMON", "Wrapped MON")
    static_words = 11
    quote_off = static_words * 32
    base_off = quote_off + len(quote_meta)
    params = [99990, 2, 10**18, 1, 2**128 - 1, 1, 30, 0]
    data = b"".join(
        [
            _addr_word(market),
            _u256(quote_off),
            _u256(base_off),
            *[_u256(v) for v in params],
            quote_meta,
            base_meta,
        ]
    )
    topics = [
        "0x" + "00" * 32,
        "0x" + ("0" * 63) + "1",
        _topic_addr(quote_asset),
        _topic_addr(base_asset),
    ]
    return topics, data.hex()


def _vault_created_data(
    vault_addr: str,
    quote_asset: str,
    base_asset: str,
    owner: str,
    max_shares: int,
    lockup: int,
    decrease_on_withdraw: bool,
    metadata: list[str] | None,
) -> tuple[list[str], str]:
    words = [
        _addr_word(quote_asset),
        _addr_word(base_asset),
        _addr_word(owner),
        _u256(max_shares),
        _u256(lockup),
        _u256(1 if decrease_on_withdraw else 0),
    ]
    if metadata is None:
        payload = b"".join(words)
    else:
        meta = _vault_meta_tuple(metadata)
        off = (len(words) + 1) * 32
        payload = b"".join([*words, _u256(off), meta])
    topics = ["0x" + "00" * 32, _topic_addr(vault_addr)]
    return topics, payload.hex()


def _vault_row(
    vault="0xvault",
    quote="0xquote",
    base="0xbase",
    market="0xmarket",
    owner="0xowner",
    name="Vault",
    description="desc",
    social1="s1",
    social2="s2",
    social3="s3",
    locked=False,
    closed=False,
    max_shares=1000,
    circulating_shares=100,
    quote_decimals=6,
    base_decimals=18,
    lockup=0,
    decrease_on_withdraw=False,
):
    return (
        vault,
        quote,
        base,
        market,
        owner,
        name,
        description,
        social1,
        social2,
        social3,
        locked,
        closed,
        max_shares,
        circulating_shares,
        quote_decimals,
        base_decimals,
        lockup,
        decrease_on_withdraw,
    )


def _vault_list_row(
    vault="0xvault",
    owner="0xowner",
    quote="0xquote",
    base="0xbase",
    market="0xmarket",
    name="Vault",
    description="desc",
    social1="s1",
    social2="s2",
    social3="s3",
    locked=False,
    closed=False,
    max_shares=1000,
    circulating_shares=100,
    quote_decimals=6,
    base_decimals=18,
    lockup=0,
    decrease_on_withdraw=False,
    latest_block=123,
    latest_ts=456,
    latest_quote_balance=1000,
    latest_base_balance=2000,
    latest_usd_value=3.5,
    user_shares=25,
    last_deposit=999,
    quote_ticker="USDC",
    base_ticker="WMON",
    quote_name="USD Coin",
    base_name="Wrapped MON",
    total_count=1,
):
    return (
        vault,
        owner,
        quote,
        base,
        market,
        name,
        description,
        social1,
        social2,
        social3,
        locked,
        closed,
        max_shares,
        circulating_shares,
        quote_decimals,
        base_decimals,
        lockup,
        decrease_on_withdraw,
        latest_block,
        latest_ts,
        latest_quote_balance,
        latest_base_balance,
        latest_usd_value,
        user_shares,
        last_deposit,
        quote_ticker,
        base_ticker,
        quote_name,
        base_name,
        total_count,
    )


def _assert_http_exc(fn, status: int):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == status
        return e
    raise AssertionError("expected HTTPException")


class _BadDecoded:
    def decode(self, *args, **kwargs):
        raise ValueError("decode fail")


class _FakeBuf:
    def __len__(self):
        return 64

    def __getitem__(self, key):
        if isinstance(key, slice):
            if key.start == 0 and key.stop == 32:
                return (1).to_bytes(32, "big")
            if key.start == 32 and key.stop == 33:
                return _BadDecoded()
        return b""


class _SqlCur:
    def __init__(self, *, fetchone_result=None, fetchall_result=None):
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result if fetchall_result is not None else []
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


class _HttpxResp:
    def __init__(self, payload, *, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("http status")

    def json(self):
        return self._payload


def test_vaults_module_helpers():
    assert vaults.to_addr("abcdef") == "0xabcdef"
    assert vaults.to_addr(bytes.fromhex("00" * 19 + "12")) == "0x" + ("00" * 19 + "12")
    assert list(vaults.chunks("abcdef", 2)) == ["ab", "cd", "ef"]
    assert vaults.u256_at(_u256(123), 0) == 123
    assert vaults.u256_at(b"", 0) == 0
    buf = _u256(4) + b"ab\x00c" + (b"\x00" * 28)
    assert vaults.read_str(buf, 0, 0) == "abc"
    assert vaults.read_str(buf, 10_000, 0) == ""
    assert vaults.read_str(_FakeBuf(), 0, 0) == ""


def test_parse_vault_created_with_metadata():
    topics, data_hex = _vault_created_data(
        vault_addr="0x6404279d0d099c5af295aa5fbad05e3c8af8e0c7",
        quote_asset="0x754704bc059f8c67012fed69bc8a327a5aafb603",
        base_asset="0x3bd359c1119da7da1d913d1c4d2b7c461115433a",
        owner="0x25afd36012fa25336cc56a1b26c56e92dd77f0f3",
        max_shares=2_000_000_000_000,
        lockup=3600,
        decrease_on_withdraw=True,
        metadata=["BZ Labs", "desc", "https://x", "https://t", "site\x00"],
    )
    out = vaults.parse_vault_created("0xfactory", topics, data_hex)
    assert out["vault"] == "0x6404279d0d099c5af295aa5fbad05e3c8af8e0c7"
    assert out["quoteAsset"] == "0x754704bc059f8c67012fed69bc8a327a5aafb603"
    assert out["baseAsset"] == "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
    assert out["owner"] == "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
    assert out["maxShares"] == 2_000_000_000_000
    assert out["lockup"] == 3600
    assert out["decreaseOnWithdraw"] is True
    assert out["locked"] is False
    assert out["closed"] is False
    assert out["metadata"]["name"] == "BZ Labs"
    assert out["metadata"]["description"] == "desc"
    assert out["metadata"]["social1"] == "https://x"
    assert out["metadata"]["social2"] == "https://t"
    assert out["metadata"]["social3"] == "site"


def test_parse_vault_created_without_metadata_and_with_bad_metadata_hex():
    topics, data_hex = _vault_created_data(
        vault_addr="0x1111111111111111111111111111111111111111",
        quote_asset="0x2222222222222222222222222222222222222222",
        base_asset="0x3333333333333333333333333333333333333333",
        owner="0x4444444444444444444444444444444444444444",
        max_shares=10,
        lockup=0,
        decrease_on_withdraw=False,
        metadata=None,
    )
    out = vaults.parse_vault_created("0xfactory", topics, data_hex)
    assert out["metadata"] == {"name": "", "description": "", "social1": "", "social2": "", "social3": ""}
    assert out["decreaseOnWithdraw"] is False

    topics2, data_hex2 = _vault_created_data(
        vault_addr="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        quote_asset="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        base_asset="0xcccccccccccccccccccccccccccccccccccccccc",
        owner="0xdddddddddddddddddddddddddddddddddddddddd",
        max_shares=1,
        lockup=2,
        decrease_on_withdraw=False,
        metadata=["n", "d", "1", "2", "3"],
    )
    bad = data_hex2[:-2] + "zz"
    out2 = vaults.parse_vault_created("0xfactory", topics2, bad)
    assert out2["vault"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert out2["metadata"] == {"name": "", "description": "", "social1": "", "social2": "", "social3": ""}


def test_parse_vault_deposit_withdraw_and_status_events_and_admin_changes():
    vault_addr = "0x6404279d0d099c5af295aa5fbad05e3c8af8e0c7"
    sender_addr = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
    topics = ["0x" + "00" * 32, _topic_addr(vault_addr), _topic_addr(sender_addr)]
    body = (_u256(123) + _u256(456) + _u256(789)).hex()

    dep = vaults.parse_vault_deposit("0xfactory", topics, body)
    wdr = vaults.parse_vault_withdraw("0xfactory", topics, body)
    for out in (dep, wdr):
        assert out["vault"] == vault_addr
        assert out["sender"] == sender_addr
        assert out["shares"] == 123
        assert out["quoteAmount"] == 456
        assert out["baseAmount"] == 789

    odd = vaults.parse_vault_deposit("0xfactory", ["0x0"], "1")
    assert odd["vault"] == ""
    assert odd["sender"] == ""
    assert odd["shares"] == 1
    assert odd["quoteAmount"] == 0
    assert odd["baseAmount"] == 0

    assert vaults.parse_vault_lock("0xfactory", ["0x0", _topic_addr(vault_addr)], "") == {"vault": vault_addr}
    assert vaults.parse_vault_unlock("0xfactory", ["0x0", _topic_addr(vault_addr)], "") == {"vault": vault_addr}
    assert vaults.parse_vault_close("0xfactory", ["0x0", _topic_addr(vault_addr)], "") == {"vault": vault_addr}
    assert vaults.parse_vault_lock("0xfactory", ["0x0"], "") == {"vault": ""}

    body_one = _u256(999).hex()
    assert vaults.parse_vault_max_shares_changed("0xfactory", ["0x0", _topic_addr(vault_addr)], body_one) == {
        "vault": vault_addr,
        "maxShares": 999,
    }
    assert vaults.parse_vault_lockup_changed("0xfactory", ["0x0", _topic_addr(vault_addr)], body_one) == {
        "vault": vault_addr,
        "lockup": 999,
    }
    assert (
        vaults.parse_vault_decrease_on_withdraw_changed("0xfactory", ["0x0", _topic_addr(vault_addr)], body_one)[
            "decreaseOnWithdraw"
        ]
        is True
    )
    assert vaults.parse_vault_decrease_on_withdraw_changed("0xfactory", ["0x0"], "") == {
        "vault": "",
        "decreaseOnWithdraw": False,
    }


def test_parse_market_created_dynamic_offsets_decode_correctly():
    topics, data_hex = _market_created_data()
    out = markets.parse_market_created("0x2cd24c8230618e26c149dce9cfb3fbb3d0a9ed54", topics, data_hex)
    assert out["market"] == "0x8a34f54f0f5f2fedfe2beb1e46cfc7a25c0df99b"
    assert out["isCanonical"] is True
    assert out["quoteAsset"] == "0x754704bc059f8c67012fed69bc8a327a5aafb603"
    assert out["baseAsset"] == "0x00000000efe302beaa2b3e6e1b18d08d69a9012a"
    assert out["quoteAddress"] == out["quoteAsset"]
    assert out["baseAddress"] == out["baseAsset"]
    assert out["quoteDecimals"] == 6
    assert out["baseDecimals"] == 18
    assert out["quoteTicker"] == "USDC"
    assert out["baseTicker"] == "WMON"
    assert out["quoteName"] == "USD Coin"
    assert out["baseName"] == "Wrapped MON"
    assert out["marketId"] == 99990
    assert out["marketType"] == 2


def test_vault_snapshot_from_samples_timeframes_and_raw_default_behavior():
    now_ts = 2_000_000_000
    rows = []
    for i in range(120):
        ts = now_ts - 86400 + i * 30
        rows.append((1000 + i, ts, 0, 0, float(i)))

    with patch.object(vault_api.time, "time", return_value=float(now_ts)):
        with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=rows) as m:
            snap = vault_api._vault_snapshot_from_samples("0xabc", timeframe=1)
    assert snap is not None
    assert len(snap["tvl"]) == len(rows)
    assert snap["tvl"][0] == [rows[0][1], float(rows[0][4])]
    assert snap["tvl"][-1] == [rows[-1][1], float(rows[-1][4])]
    assert snap["stats"]["pctChange"] == 0.0
    m.assert_called_once_with("0xabc", start_ts=now_ts - 86400, limit=0)

    for tf, delta in ((2, 7 * 86400), (3, 30 * 86400), (4, None)):
        with patch.object(vault_api.time, "time", return_value=float(now_ts)):
            with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=rows) as m2:
                vault_api._vault_snapshot_from_samples("0xabc", timeframe=tf)
        expect_start = None if delta is None else now_ts - delta
        m2.assert_called_once_with("0xabc", start_ts=expect_start, limit=0)


def test_vault_snapshot_from_samples_optional_downsampling_and_empty_cases():
    rows = [(i, 1000 + i, 0, 0, float(i)) for i in range(6)]
    with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=[]):
        assert vault_api._vault_snapshot_from_samples("0xabc", timeframe=4) is None

    with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=rows):
        snap2 = vault_api._vault_snapshot_from_samples("0xabc", timeframe=4, points=2)
        snap4 = vault_api._vault_snapshot_from_samples("0xabc", timeframe=4, points=4)
        snap_zero = vault_api._vault_snapshot_from_samples("0xabc", timeframe=4, points=4)

    assert len(snap2["tvl"]) == 2
    assert snap2["tvl"][0][1] == 0.0
    assert snap2["tvl"][-1][1] == 5.0
    assert len(snap4["tvl"]) <= 4
    assert snap4["tvl"][0][1] == 0.0
    assert snap4["tvl"][-1][1] == 5.0
    assert snap_zero["stats"]["pctChange"] == 0.0


def test_vault_snapshot_from_samples_pct_when_first_value_zero():
    rows = [
        (1, 1000, 0, 0, 0.0),
        (2, 1010, 0, 0, 5.0),
    ]
    with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=rows):
        snap = vault_api._vault_snapshot_from_samples("0xabc", timeframe=4)
    assert snap["stats"]["pctChange"] == 0.0
    assert snap["stats"]["lastUsd"] == 5.0


def test_vault_user_lockup_fields_helper():
    out_empty = vault_api._vault_user_lockup_fields(now_ts=100, lockup_seconds=3600, user_row=None, user_shares=0)
    assert out_empty == {
        "lastDeposit": 0,
        "lastWithdraw": 0,
        "withdrawLocked": False,
        "withdrawUnlockAt": 0,
        "withdrawLockupRemaining": 0,
    }

    out_active = vault_api._vault_user_lockup_fields(
        now_ts=1_000,
        lockup_seconds=600,
        user_row=(5, 1, 0, 900, 800),
        user_shares=5,
    )
    assert out_active["lastDeposit"] == 900
    assert out_active["lastWithdraw"] == 800
    assert out_active["withdrawUnlockAt"] == 1500
    assert out_active["withdrawLocked"] is True
    assert out_active["withdrawLockupRemaining"] == 500

    out_unlocked = vault_api._vault_user_lockup_fields(
        now_ts=2_000,
        lockup_seconds=600,
        user_row=(5, 1, 0, 900, 800),
        user_shares=5,
    )
    assert out_unlocked["withdrawLocked"] is False
    assert out_unlocked["withdrawLockupRemaining"] == 0


def test_vault_user_summary_not_found_and_fallback_snapshot():
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=None):
        _assert_http_exc(
            lambda: vault_api.vault_user_summary(
                "0xvault",
                "0x0000000000000000000000000000000000000000",
                history_limit=1,
                snapshot_timeframe=1,
            ),
            404,
        )

    latest_row = (100, 1000, 10, 20, 12.34)
    with ExitStack() as st:
        st.enter_context(
            patch.object(vault_api.storage, "get_crystal_vault", return_value=_vault_row(circulating_shares=0))
        )
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_latest_balance", return_value=latest_row))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_user", return_value=None))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_deposits", return_value=[]))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_withdrawals", return_value=[]))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_users", return_value=[]))
        st.enter_context(patch.object(vault_api, "_vault_snapshot_from_samples", return_value=None))
        out2 = vault_api.vault_user_summary(
            "0xvault",
            "0x0000000000000000000000000000000000000000",
            history_limit=1,
            snapshot_timeframe=1,
        )
    assert out2["ok"] is True
    assert out2["userBalance"]["shares"] == 0
    assert out2["userBalance"]["quoteBalance"] == 0
    assert out2["snapshot"]["timeframe"] == 1
    assert out2["snapshot"]["tvl"] == []
    assert out2["snapshot"]["stats"]["lastUsd"] == 12.34
    assert out2["snapshot"]["stats"]["pctChange"] == 0.0


def test_vault_user_summary_handles_missing_latest_balance_row():
    with ExitStack() as st:
        st.enter_context(
            patch.object(vault_api.storage, "get_crystal_vault", return_value=_vault_row(circulating_shares=10))
        )
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_latest_balance", return_value=None))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_user", return_value=(1, 0, 0, 123, 0)))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_deposits", return_value=[]))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_withdrawals", return_value=[]))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_users", return_value=[]))
        st.enter_context(patch.object(vault_api, "_vault_snapshot_from_samples", return_value=None))
        st.enter_context(patch.object(vault_api.time, "time", return_value=200.0))
        out = vault_api.vault_user_summary(
            "0xvault",
            "0xuser",
            history_limit=5,
            snapshot_timeframe=1,
        )
    assert out["latestBalance"] == {
        "quoteBalance": 0,
        "baseBalance": 0,
        "timestamp": 0,
        "usdValue": 0.0,
        "block": 0,
    }
    assert out["tvlUsd"] == 0.0
    assert out["snapshot"] is None
    assert out["userBalance"]["shares"] == 1
    assert out["userBalance"]["sharePct"] == 0.1
    assert out["userBalance"]["lastDeposit"] == 123
    assert out["userBalance"]["withdrawLocked"] is False


def test_vault_user_summary_full_path_maps_lists_and_computes_share_balances():
    vault_row = _vault_row(
        vault="0xVaUlT",
        quote="0xQ",
        base="0xB",
        market="0xM",
        owner="0xOwner",
        name="My Vault",
        description="hello",
        social1="a",
        social2="b",
        social3="c",
        locked=True,
        closed=False,
        max_shares=1000,
        circulating_shares=200,
        quote_decimals=6,
        base_decimals=18,
        lockup=777,
        decrease_on_withdraw=True,
    )
    latest_row = (123, 456, 1000, 5000, 99.5)
    user_row = (50, 1, 0, 400, 300)
    deps = [("0xD1", 111, 10, 20, 30, "0xhash1")]
    wds = [("0xD2", 222, 1, 2, 3, "0xhash2")]
    users = [("0xD1", 50, 1, 0, 111, 0), ("0xD2", 150, 2, 1, 200, 222)]
    snapshot = {"timeframe": 1, "tvl": [[1, 10.0], [2, 20.0]], "stats": {"pctChange": 100.0, "lastUsd": 20.0}}

    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_latest_balance", return_value=latest_row))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_user", return_value=user_row))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_deposits", return_value=deps))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_withdrawals", return_value=wds))
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vault_users", return_value=users))
        st.enter_context(patch.object(vault_api, "_vault_snapshot_from_samples", return_value=snapshot))
        st.enter_context(patch.object(vault_api.time, "time", return_value=500.0))
        out = vault_api.vault_user_summary("0xVAULT", "0xUSER", history_limit=5, snapshot_timeframe=2)

    assert out["ok"] is True
    assert out["vault"]["vault"] == "0xVaUlT"
    assert out["vault"]["params"]["maxShares"] == 1000
    assert out["vault"]["params"]["circulatingShares"] == 200
    assert out["vault"]["params"]["lockup"] == 777
    assert out["vault"]["params"]["decreaseOnWithdraw"] is True
    assert out["status"] == {"locked": True, "closed": False}
    assert out["latestBalance"]["usdValue"] == 99.5
    assert out["tvlUsd"] == 99.5
    assert out["userBalance"]["address"] == "0xuser"
    assert out["userBalance"]["shares"] == 50
    assert out["userBalance"]["sharePct"] == 0.25
    assert out["userBalance"]["quoteBalance"] == 250
    assert out["userBalance"]["baseBalance"] == 1250
    assert out["userBalance"]["lastDeposit"] == 400
    assert out["userBalance"]["lastWithdraw"] == 300
    assert out["userBalance"]["withdrawLocked"] is True
    assert out["userBalance"]["withdrawUnlockAt"] == 1177
    assert out["userBalance"]["withdrawLockupRemaining"] == 677
    assert out["depositHistory"][0]["user"] == "0xd1"
    assert out["withdrawHistory"][0]["user"] == "0xd2"
    assert len(out["depositors"]) == 2
    assert out["depositors"][0]["address"] == "0xd1"
    assert out["depositors"][0]["sharePct"] == 0.25
    assert out["depositors"][1]["sharePct"] == 0.75
    assert out["snapshot"] == snapshot


def test_vaults_list_invalid_status_and_empty_page():
    _assert_http_exc(lambda: vault_api.list_vaults(status="bad"), 400)
    _assert_http_exc(lambda: vault_api.list_vaults(status="all", sort="bad", order="desc"), 400)
    _assert_http_exc(lambda: vault_api.list_vaults(status="all", sort="latest_deposit", order="sideways"), 400)

    with patch.object(vault_api.storage, "list_crystal_vaults_page", return_value=[]) as m:
        out = vault_api.list_vaults(
            user=None,
            search="",
            status="all",
            page=1,
            limit=50,
            include_snapshot=True,
            snapshot_timeframe=1,
            snapshot_points=48,
        )
    m.assert_called_once()
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["total"] == 0
    assert out["hasMore"] is False
    assert out["vaults"] == []


def test_vaults_list_custom_sort_and_order_are_forwarded():
    rows = [_vault_list_row(total_count=1)]
    with patch.object(vault_api.storage, "list_crystal_vaults_page", return_value=rows) as m:
        out = vault_api.list_vaults(
            user="0xUSER",
            search="",
            status="all",
            sort="user_position",
            order="asc",
            page=1,
            limit=50,
            include_snapshot=False,
            snapshot_timeframe=1,
            snapshot_points=48,
        )
    m.assert_called_once_with(
        user_address="0xuser",
        search="",
        status="all",
        page=1,
        limit=50,
        sort_by="user_position",
        sort_dir="asc",
    )
    assert out["filters"]["sort"] == "user_position"
    assert out["filters"]["order"] == "asc"


def test_vaults_list_maps_rows_and_snapshots():
    rows = [
        _vault_list_row(
            vault="0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa",
            owner="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            quote="0xQ",
            base="0xB",
            market="0xM",
            name="BZ Labs",
            locked=True,
            closed=False,
            max_shares=2_000_000_000_000,
            circulating_shares=200,
            latest_quote_balance=10000,
            latest_base_balance=50000,
            latest_usd_value=12.34,
            user_shares=50,
            total_count=3,
        ),
        _vault_list_row(
            vault="0xcccccccccccccccccccccccccccccccccccccccc",
            owner="0xdddddddddddddddddddddddddddddddddddddddd",
            name="Other",
            closed=True,
            latest_usd_value=0.0,
            user_shares=0,
            total_count=3,
        ),
    ]
    snap = {"timeframe": 1, "tvl": [[1, 1.0], [2, 2.0]], "stats": {"pctChange": 100.0, "lastUsd": 2.0}}
    with ExitStack() as st:
        m = st.enter_context(patch.object(vault_api.storage, "list_crystal_vaults_page", return_value=rows))
        s = st.enter_context(patch.object(vault_api, "_vault_snapshot_from_samples", side_effect=[snap, None]))
        out = vault_api.list_vaults(
            user="0xUSER",
            search="bz",
            status="active",
            page=2,
            limit=2,
            include_snapshot=True,
            snapshot_timeframe=1,
            snapshot_points=48,
        )

    m.assert_called_once_with(
        user_address="0xuser",
        search="bz",
        status="active",
        page=2,
        limit=2,
        sort_by="latest_deposit",
        sort_dir="desc",
    )
    assert s.call_count == 2
    first = out["vaults"][0]
    second = out["vaults"][1]
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["total"] == 3
    assert out["page"] == 2
    assert out["limit"] == 2
    assert out["hasMore"] is False
    assert out["filters"]["status"] == "active"
    assert out["filters"]["user"] == "0xuser"
    assert first["address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert first["owner"] == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert first["type"] == "Spot"
    assert first["maxShares"] == "2000000000000"
    assert first["totalShares"] == "200"
    assert first["userShares"] == "50"
    assert first["userSharePct"] == 0.25
    assert first["quoteBalance"] == "10000"
    assert first["baseBalance"] == "50000"
    assert first["tvlUsd"] == 12.34
    assert first["snapshot"] == snap
    assert second["closed"] is True
    assert second["snapshot"] is None


def test_vaults_list_snapshot_fallback_and_disable_snapshot():
    rows = [_vault_list_row(latest_usd_value=9.99, total_count=1)]
    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vaults_page", return_value=rows))
        st.enter_context(patch.object(vault_api, "_vault_snapshot_from_samples", return_value=None))
        out = vault_api.list_vaults(
            user=None,
            search="",
            status="all",
            page=1,
            limit=50,
            include_snapshot=True,
            snapshot_timeframe=2,
            snapshot_points=10,
        )
    assert out["vaults"][0]["snapshot"]["timeframe"] == 2
    assert out["vaults"][0]["snapshot"]["stats"]["lastUsd"] == 9.99

    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "list_crystal_vaults_page", return_value=rows))
        s = st.enter_context(patch.object(vault_api, "_vault_snapshot_from_samples", return_value={"x": 1}))
        out2 = vault_api.list_vaults(
            user=None,
            search="",
            status="all",
            page=1,
            limit=50,
            include_snapshot=False,
            snapshot_timeframe=1,
            snapshot_points=48,
        )
    assert out2["vaults"][0]["snapshot"] is None
    s.assert_not_called()


def test_vault_refresh_balance_not_found_and_rpc_failure():
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=None):
        _assert_http_exc(lambda: vault_api.vault_refresh_balance("0xvault"), 404)

    with patch.object(vault_api.storage, "get_crystal_vault", return_value=_vault_row(circulating_shares=10)):
        with patch.object(vault_api.httpx, "post", side_effect=RuntimeError("rpc down")):
            _assert_http_exc(lambda: vault_api.vault_refresh_balance("0xvault", user="0xuser"), 502)


def test_vault_refresh_balance_success_and_user_projection():
    vault_row = _vault_row(vault="0xVaUlT", circulating_shares=200, locked=True, closed=False, lockup=100)
    call_hex = "0x" + f"{1000:064x}" + f"{5000:064x}" + ("00" * 64)
    responses = [
        _HttpxResp({"jsonrpc": "2.0", "id": 1, "result": hex(123)}),
        _HttpxResp({"jsonrpc": "2.0", "id": 1, "result": {"timestamp": hex(456)}}),
        _HttpxResp({"jsonrpc": "2.0", "id": 1, "result": call_hex}),
    ]
    fake_state = MagicMock()
    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_user", return_value=(50, 0, 0, 400, 0)))
        st.enter_context(
            patch.object(
                vault_api.storage, "get_crystal_vault_latest_balance", return_value=(123, 456, 1000, 5000, 42.25)
            )
        )
        st.enter_context(patch.object(vault_api, "State", return_value=fake_state))
        post = st.enter_context(patch.object(vault_api.httpx, "post", side_effect=responses))
        out = vault_api.vault_refresh_balance("0xVAULT", user="0xUSER")

    assert out["ok"] is True
    assert out["vault"] == "0xvault"
    assert out["latestBalance"] == {
        "block": 123,
        "timestamp": 456,
        "quoteBalance": 1000,
        "baseBalance": 5000,
        "usdValue": 42.25,
    }
    assert out["userBalance"]["address"] == "0xuser"
    assert out["userBalance"]["shares"] == 50
    assert out["userBalance"]["sharePct"] == 0.25
    assert out["userBalance"]["quoteBalance"] == 250
    assert out["userBalance"]["baseBalance"] == 1250
    assert out["userBalance"]["lastDeposit"] == 400
    assert out["userBalance"]["withdrawLocked"] is True
    assert out["userBalance"]["withdrawUnlockAt"] == 500
    assert out["userBalance"]["withdrawLockupRemaining"] == 44
    assert out["status"] == {"locked": True, "closed": False}
    # refresh-balance no longer writes samples; the indexer's sampler owns the series
    assert out["samplePersisted"] is False
    assert out["source"] == "rpc"
    assert post.call_count == 3
    fake_state.record_vault_balance_sample.assert_not_called()


def test_vault_refresh_balance_handles_bad_head_and_bad_call_and_fallback_timestamp():
    vault_row = _vault_row(vault="0xv", circulating_shares=0)
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row):
        with patch.object(vault_api.httpx, "post", return_value=_HttpxResp({"result": None})):
            _assert_http_exc(lambda: vault_api.vault_refresh_balance("0xv"), 502)

    bad_call_responses = [
        _HttpxResp({"result": hex(10)}),
        _HttpxResp({"result": "not-a-dict"}),
        _HttpxResp({"result": "nope"}),
    ]
    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row))
        st.enter_context(patch.object(vault_api.httpx, "post", side_effect=bad_call_responses))
        st.enter_context(patch.object(vault_api.time, "time", return_value=999.0))
        _assert_http_exc(lambda: vault_api.vault_refresh_balance("0xv", user=None), 502)

    ok_fallback_ts = [
        _HttpxResp({"result": hex(11)}),
        _HttpxResp({"result": {"timestamp": hex(0)}}, status_ok=False),
        _HttpxResp({"result": "0x" + f"{1:064x}" + f"{2:064x}" + ("00" * 64)}),
    ]
    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_user", return_value=None))
        st.enter_context(
            patch.object(vault_api.storage, "get_crystal_vault_latest_balance", return_value=(10, 321, 1, 2, 9.5))
        )
        st.enter_context(patch.object(vault_api, "State", side_effect=RuntimeError("state rebuild failed")))
        st.enter_context(patch.object(vault_api.httpx, "post", side_effect=ok_fallback_ts))
        st.enter_context(patch.object(vault_api.time, "time", return_value=321.0))
        out = vault_api.vault_refresh_balance("0xv")
    assert out["latestBalance"]["timestamp"] == 321
    assert out["latestBalance"]["usdValue"] == 9.5
    assert out["userBalance"] is None
    assert out["samplePersisted"] is False


def test_vault_refresh_balance_sample_write_paths_without_latest_row():
    vault_row = _vault_row(vault="0xv", circulating_shares=0)
    ok_rpc = [
        _HttpxResp({"result": hex(22)}),
        _HttpxResp({"result": {"timestamp": hex(333)}}),
        _HttpxResp({"result": "0x" + f"{7:064x}" + f"{8:064x}" + ("00" * 64)}),
    ]
    fake_state = MagicMock()
    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_user", return_value=None))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_latest_balance", return_value=None))
        st.enter_context(patch.object(vault_api, "State", return_value=fake_state))
        st.enter_context(patch.object(vault_api.httpx, "post", side_effect=ok_rpc))
        out_ok = vault_api.vault_refresh_balance("0xv")
    assert out_ok["samplePersisted"] is False
    assert out_ok["latestBalance"]["usdValue"] == 0.0

    ok_rpc_2 = [
        _HttpxResp({"result": hex(23)}),
        _HttpxResp({"result": {"timestamp": hex(334)}}),
        _HttpxResp({"result": "0x" + f"{9:064x}" + f"{10:064x}" + ("00" * 64)}),
    ]
    with ExitStack() as st:
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_user", return_value=None))
        st.enter_context(patch.object(vault_api.storage, "get_crystal_vault_latest_balance", return_value=None))
        st.enter_context(patch.object(vault_api, "State", side_effect=RuntimeError("boom")))
        st.enter_context(patch.object(vault_api.httpx, "post", side_effect=ok_rpc_2))
        out_err = vault_api.vault_refresh_balance("0xv")
    assert out_err["samplePersisted"] is False
    assert out_err["latestBalance"]["usdValue"] == 0.0


def test_vault_rpc_jsonrpc_sync_invalid_response_branches():
    with patch.object(vault_api.httpx, "post", return_value=_HttpxResp("not-a-dict")):
        try:
            vault_api._rpc_jsonrpc_sync("eth_blockNumber", [])
        except ValueError as e:
            assert "invalid rpc response" in str(e)
        else:
            raise AssertionError("expected ValueError")

    with patch.object(vault_api.httpx, "post", return_value=_HttpxResp({"error": {"code": -1}})):
        try:
            vault_api._rpc_jsonrpc_sync("eth_blockNumber", [])
        except ValueError as e:
            assert "code" in str(e)
        else:
            raise AssertionError("expected ValueError")


def test_vault_timeframe_parser_accepts_aliases_and_rejects_bad_values():
    for raw, expected in [
        (1, 1),
        ("1", 1),
        ("1d", 1),
        ("day", 1),
        ("24h", 1),
        ("2", 2),
        ("1w", 2),
        ("week", 2),
        ("7d", 2),
        ("3", 3),
        ("1m", 3),
        ("month", 3),
        ("30d", 3),
        ("4", 4),
        ("all", 4),
        ("all-time", 4),
        ("all_time", 4),
        ("alltime", 4),
        ("lifetime", 4),
    ]:
        assert vault_api._parse_timeframe(raw) == expected

    _assert_http_exc(lambda: vault_api._parse_timeframe("9"), 400)
    _assert_http_exc(lambda: vault_api._parse_timeframe("banana"), 400)
    _assert_http_exc(lambda: vault_api._parse_timeframe(True), 400)


def test_vault_history_not_found_invalid_timeframe_and_empty_series():
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=None):
        _assert_http_exc(lambda: vault_api.vault_history("0xvault", 1, limit=0), 404)
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=_vault_row()):
        _assert_http_exc(lambda: vault_api.vault_history("0xvault", 9, limit=0), 400)

    with patch.object(vault_api.storage, "get_crystal_vault", return_value=_vault_row()):
        with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=[]):
            out = vault_api.vault_history("0xvault", 4, limit=0)
    assert out["ok"] is True
    assert out["series"]["tvl"] == []
    assert out["series"]["pnl"] == []
    assert out["info"]["timeframe"] == "all"


def test_vault_history_accepts_string_timeframes():
    now_ts = 2_100_000_000
    with patch.object(vault_api.time, "time", return_value=float(now_ts)):
        with patch.object(vault_api.storage, "get_crystal_vault", return_value=_vault_row(vault="0xv")):
            with patch.object(
                vault_api.storage, "list_crystal_vault_balance_samples", return_value=[(1, 1000, 0, 0, 1.0)]
            ) as m:
                out = vault_api.vault_history("0xV", "1w", limit=0)
    m.assert_called_once_with("0xv", start_ts=now_ts - (7 * 86400), limit=0)
    assert out["info"]["timeframe"] == "week"

    with patch.object(vault_api.time, "time", return_value=float(now_ts)):
        with patch.object(vault_api.storage, "get_crystal_vault", return_value=_vault_row(vault="0xv")):
            with patch.object(
                vault_api.storage, "list_crystal_vault_balance_samples", return_value=[(1, 1000, 0, 0, 1.0)]
            ) as m2:
                out2 = vault_api.vault_history("0xV", "all-time", limit=0)
    m2.assert_called_once_with("0xv", start_ts=None, limit=0)
    assert out2["info"]["timeframe"] == "all"


def test_vault_history_timeframe_calls_and_series_math():
    vault_row = _vault_row(vault="0xv", owner="0xo", name="Vault X", circulating_shares=2000)
    pts_rows = [
        (1, 1000, 0, 0, 1.0),
        (2, 1010, 0, 0, 2.0),
        (3, 1020, 0, 0, 1.5),
    ]
    now_ts = 2_100_000_000
    for tf, tf_name, delta in ((1, "day", 86400), (2, "week", 7 * 86400), (3, "month", 30 * 86400), (4, "all", None)):
        with patch.object(vault_api.time, "time", return_value=float(now_ts)):
            with patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row):
                with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=pts_rows) as m:
                    out = vault_api.vault_history("0xV", tf, limit=77)
        expect_start = None if delta is None else now_ts - delta
        m.assert_called_once_with("0xv", start_ts=expect_start, limit=77)
        assert out["info"]["timeframe"] == tf_name
        assert [p["timestamp"] for p in out["series"]["tvl"]] == [1000, 1010, 1020]
        assert [p["tvlUsd"] for p in out["series"]["tvl"]] == [1.0, 2.0, 1.5]
        assert [p["pnlUsd"] for p in out["series"]["pnl"]] == [0.0, 0.0, 0.0]
        assert [p["pnlPct"] for p in out["series"]["pnl"]] == [0.0, 0.0, 0.0]


def test_vault_storage_upsert_vault_sql_and_cursor_paths():
    fake = _SqlCur()
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(fake)):
        vault_storage.upsert_crystal_vault(
            vault="0xVAULT",
            quote="0xQUOTE",
            base="0xBASE",
            market="0xMARKET",
            owner="0xOWNER",
            name="A\x00B",
            description="D\x00",
            social1="S1\x00",
            social2="S2",
            social3="S3",
            locked=True,
            closed=False,
            max_shares=10,
            circulating_shares=20,
            quote_decimals=6,
            base_decimals=18,
            lockup=30,
            decrease_on_withdraw=True,
            deployed_block=100,
            deployed_at=200,
            updated_block=300,
            updated_at=400,
        )
    ((sql, params),) = fake.exec_calls
    assert "INSERT INTO crystal_vaults" in sql
    assert params[0] == "0xvault"
    assert params[1] == "0xquote"
    assert params[2] == "0xbase"
    assert params[3] == "0xmarket"
    assert params[4] == "0xowner"
    assert params[5] == "AB"
    assert params[6] == "D"
    assert params[7] == "S1"
    assert params[17] is True
    assert params[18:22] == (100, 200, 300, 400)

    cur = MagicMock()
    vault_storage.upsert_crystal_vault(
        vault="0xVAULT",
        quote="0xQUOTE",
        base="0xBASE",
        market="",
        owner="0xOWNER",
        name="",
        description="",
        social1="",
        social2="",
        social3="",
        locked=False,
        closed=True,
        max_shares=0,
        circulating_shares=0,
        quote_decimals=0,
        base_decimals=0,
        lockup=0,
        decrease_on_withdraw=False,
        deployed_block=None,
        deployed_at=None,
        updated_block=None,
        updated_at=None,
        cur=cur,
    )
    sql2, params2 = cur.execute.call_args[0]
    assert "INSERT INTO crystal_vaults" in sql2
    assert params2[3] == ""
    assert params2[12:17] == (0, 0, 0, 0, 0)
    assert params2[18:22] == (None, None, None, None)


def test_vault_storage_update_fields_sql_and_cursor_paths():
    fake = _SqlCur()
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(fake)):
        vault_storage.update_crystal_vault_fields(
            vault="0xVAULT",
            locked=True,
            closed=False,
            max_shares=123,
            circulating_shares=456,
            lockup=789,
            decrease_on_withdraw=True,
            market="0xMARKET",
            quote_decimals=6,
            base_decimals=18,
            updated_block=11,
            updated_at=22,
        )
    ((sql, params),) = fake.exec_calls
    assert "UPDATE crystal_vaults" in sql
    assert params[:-1] == (True, False, 123, 456, 789, True, "0xMARKET", 6, 18, 11, 22)
    assert params[-1] == "0xvault"

    cur = MagicMock()
    vault_storage.update_crystal_vault_fields(
        vault="0xVAULT",
        locked=None,
        closed=None,
        max_shares=None,
        circulating_shares=None,
        lockup=None,
        decrease_on_withdraw=None,
        market=None,
        quote_decimals=None,
        base_decimals=None,
        updated_block=None,
        updated_at=None,
        cur=cur,
    )
    _, params2 = cur.execute.call_args[0]
    assert params2[:-1] == (None, None, None, None, None, None, None, None, None, None, None)
    assert params2[-1] == "0xvault"


def test_vault_storage_event_and_user_write_helpers_sql_and_cursor_paths():
    dep_cur = _SqlCur()
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(dep_cur)):
        vault_storage.insert_crystal_vault_deposit(
            block_number=1,
            log_index=2,
            timestamp=3,
            vault="0xVAULT",
            user_address="0xUSER",
            shares=4,
            quote_amount=5,
            base_amount=6,
            txhash="0xHASH",
        )
    ((dep_sql, dep_params),) = dep_cur.exec_calls
    assert "INSERT INTO crystal_vault_deposits" in dep_sql
    assert dep_params == (1, 2, 3, "0xvault", "0xuser", 4, 5, 6, "0xhash")
    dep_direct = MagicMock()
    vault_storage.insert_crystal_vault_deposit(
        block_number=0,
        log_index=0,
        timestamp=0,
        vault="0xVAULT",
        user_address="0xUSER",
        shares=0,
        quote_amount=0,
        base_amount=0,
        txhash="0xHASH",
        cur=dep_direct,
    )
    assert dep_direct.execute.call_count == 1

    wd_cur = _SqlCur()
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(wd_cur)):
        vault_storage.insert_crystal_vault_withdrawal(
            block_number=7,
            log_index=8,
            timestamp=9,
            vault="0xVAULT",
            user_address="0xUSER",
            shares=10,
            quote_amount=11,
            base_amount=12,
            txhash="0xHASH2",
        )
    ((wd_sql, wd_params),) = wd_cur.exec_calls
    assert "INSERT INTO crystal_vault_withdrawals" in wd_sql
    assert wd_params == (7, 8, 9, "0xvault", "0xuser", 10, 11, 12, "0xhash2")
    wd_direct = MagicMock()
    vault_storage.insert_crystal_vault_withdrawal(
        block_number=0,
        log_index=0,
        timestamp=0,
        vault="0xVAULT",
        user_address="0xUSER",
        shares=0,
        quote_amount=0,
        base_amount=0,
        txhash="0xHASH2",
        cur=wd_direct,
    )
    assert wd_direct.execute.call_count == 1

    user_cur = _SqlCur()
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(user_cur)):
        vault_storage.upsert_crystal_vault_user_delta(
            vault="0xVAULT",
            user_address="0xUSER",
            shares_delta=5,
            deposits_delta=1,
            withdraws_delta=2,
            last_deposit=123,
            last_withdraw=456,
        )
    ((user_sql, user_params),) = user_cur.exec_calls
    assert "INSERT INTO crystal_vault_users" in user_sql
    assert user_params == ("0xvault", "0xuser", 5, 1, 2, 123, 456, 5)
    user_direct = MagicMock()
    vault_storage.upsert_crystal_vault_user_delta(
        vault="0xVAULT",
        user_address="0xUSER",
        shares_delta=0,
        deposits_delta=0,
        withdraws_delta=0,
        last_deposit=None,
        last_withdraw=None,
        cur=user_direct,
    )
    _, user_params2 = user_direct.execute.call_args[0]
    assert user_params2 == ("0xvault", "0xuser", 0, 0, 0, None, None, 0)

    bal_cur = _SqlCur()
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(bal_cur)):
        vault_storage.upsert_crystal_vault_balance_sample(
            vault="0xVAULT",
            block_number=99,
            timestamp=100,
            quote_balance=101,
            base_balance=102,
            usd_value="12.34",
        )
    ((bal_sql, bal_params),) = bal_cur.exec_calls
    assert "INSERT INTO crystal_vault_balance_samples" in bal_sql
    assert bal_params[:5] == ("0xvault", 99, 100, 101, 102)
    assert bal_params[5] == Decimal("12.34")
    bal_direct = MagicMock()
    vault_storage.upsert_crystal_vault_balance_sample(
        vault="0xVAULT",
        block_number=0,
        timestamp=0,
        quote_balance=0,
        base_balance=0,
        usd_value=None,
        cur=bal_direct,
    )
    _, bal_params2 = bal_direct.execute.call_args[0]
    assert bal_params2[5] == Decimal("0")


def test_vault_storage_read_helpers_issue_expected_queries():
    cur1 = _SqlCur(fetchall_result=[("v",)])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur1)):
        assert vault_storage.load_crystal_vaults_for_state() == [("v",)]
    assert "FROM crystal_vaults" in cur1.exec_calls[0][0]

    cur2 = _SqlCur(fetchall_result=[("u",)])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur2)):
        assert vault_storage.load_crystal_vault_users_for_state() == [("u",)]
    assert "FROM crystal_vault_users" in cur2.exec_calls[0][0]

    cur3 = _SqlCur(fetchone_result=("row",))
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur3)):
        assert vault_storage.get_crystal_vault("0xVAULT") == ("row",)
    assert cur3.exec_calls[0][1] == ("0xvault",)

    cur4 = _SqlCur(fetchone_result=("bal",))
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur4)):
        assert vault_storage.get_crystal_vault_latest_balance("0xVAULT") == ("bal",)
    assert "ORDER BY timestamp DESC, block_number DESC" in cur4.exec_calls[0][0]
    assert cur4.exec_calls[0][1] == ("0xvault",)

    cur5 = _SqlCur(fetchone_result=("user",))
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur5)):
        assert vault_storage.get_crystal_vault_user("0xVAULT", "0xUSER") == ("user",)
    assert cur5.exec_calls[0][1] == ("0xvault", "0xuser")

    cur6 = _SqlCur(fetchall_result=[("d",)])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur6)):
        assert vault_storage.list_crystal_vault_deposits("0xVAULT", limit=7) == [("d",)]
    assert "FROM crystal_vault_deposits" in cur6.exec_calls[0][0]
    assert cur6.exec_calls[0][1] == ("0xvault", 7)

    cur7 = _SqlCur(fetchall_result=[("w",)])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur7)):
        assert vault_storage.list_crystal_vault_withdrawals("0xVAULT", limit=8) == [("w",)]
    assert "FROM crystal_vault_withdrawals" in cur7.exec_calls[0][0]
    assert cur7.exec_calls[0][1] == ("0xvault", 8)

    cur8 = _SqlCur(fetchall_result=[("u",)])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur8)):
        assert vault_storage.list_crystal_vault_users("0xVAULT") == [("u",)]
    assert "ORDER BY last_deposit DESC, shares DESC, user_address ASC" in cur8.exec_calls[0][0]
    assert cur8.exec_calls[0][1] == ("0xvault",)


def test_vault_storage_list_vaults_page_builds_sql_for_filters_and_sorts():
    cur_default = _SqlCur(fetchall_result=[("row",)])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_default)):
        rows = vault_storage.list_crystal_vaults_page(
            user_address="0xUSER",
            search="  bz  ",
            status="all",
            page=3,
            limit=20,
            sort_by="latest_deposit",
            sort_dir="desc",
        )
    assert rows == [("row",)]
    sql0, params0 = cur_default.exec_calls[0]
    assert "AND v.closed = FALSE" not in sql0 and "AND v.closed = TRUE" not in sql0
    assert "COALESCE(ld.last_deposit, v.updated_at, v.deployed_at, 0) DESC NULLS LAST" in sql0
    assert params0 == ("0xuser", "bz", "%bz%", "%bz%", "%bz%", 20, 40)

    cur_active = _SqlCur(fetchall_result=[])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_active)):
        vault_storage.list_crystal_vaults_page(
            user_address="0xUSER",
            search="",
            status="active",
            page=1,
            limit=50,
            sort_by="tvl",
            sort_dir="asc",
        )
    sql1, params1 = cur_active.exec_calls[0]
    assert "AND v.closed = FALSE" in sql1
    assert "COALESCE(lb.usd_value, 0.0) ASC NULLS LAST" in sql1
    assert params1 == ("0xuser", "", "%%", "%%", "%%", 50, 0)

    cur_closed = _SqlCur(fetchall_result=[])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_closed)):
        vault_storage.list_crystal_vaults_page(
            user_address="0xUSER",
            search=None,
            status="closed",
            page=2,
            limit=10,
            sort_by="user_position",
            sort_dir="desc",
        )
    sql2, params2 = cur_closed.exec_calls[0]
    assert "AND v.closed = TRUE" in sql2
    assert "COALESCE(uv.shares, 0)::double precision" in sql2
    assert "DESC NULLS LAST" in sql2
    assert params2 == ("0xuser", "", "%%", "%%", "%%", 10, 10)

    cur_invalid = _SqlCur(fetchall_result=[])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_invalid)):
        vault_storage.list_crystal_vaults_page(
            user_address=None,
            search="x",
            status="weird",
            page=0,
            limit=999,
            sort_by="wat",
            sort_dir="sideways",
        )
    sql3, params3 = cur_invalid.exec_calls[0]
    assert "AND v.closed = FALSE" not in sql3 and "AND v.closed = TRUE" not in sql3
    assert "COALESCE(ld.last_deposit, v.updated_at, v.deployed_at, 0) DESC NULLS LAST" in sql3
    assert params3 == ("", "x", "%x%", "%x%", "%x%", 200, 0)

    cur_zero_limit = _SqlCur(fetchall_result=[])
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_zero_limit)):
        vault_storage.list_crystal_vaults_page(
            user_address="0xUSER",
            search="",
            status="all",
            page=1,
            limit=0,
        )
    _, params4 = cur_zero_limit.exec_calls[0]
    assert params4[-2:] == (50, 0)


def test_vault_storage_list_balance_samples_query_variants_and_ordering():
    rows_a = [(2, 20, 0, 0, 2.0), (1, 10, 0, 0, 1.0)]
    cur_a = _SqlCur(fetchall_result=rows_a)
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_a)):
        out_a = vault_storage.list_crystal_vault_balance_samples("0xVAULT", start_ts=None, limit=2)
    sql_a, params_a = cur_a.exec_calls[0]
    assert "WHERE vault = %s" in sql_a and "LIMIT %s" in sql_a
    assert params_a == ("0xvault", 2)
    assert out_a == list(reversed(rows_a))

    rows_b = [(3, 30, 0, 0, 3.0)]
    cur_b = _SqlCur(fetchall_result=rows_b)
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_b)):
        out_b = vault_storage.list_crystal_vault_balance_samples("0xVAULT", start_ts=None, limit=0)
    sql_b, params_b = cur_b.exec_calls[0]
    assert "WHERE vault = %s" in sql_b and "LIMIT %s" not in sql_b
    assert params_b == ("0xvault",)
    assert out_b == list(reversed(rows_b))

    rows_c = [(4, 40, 0, 0, 4.0)]
    cur_c = _SqlCur(fetchall_result=rows_c)
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_c)):
        out_c = vault_storage.list_crystal_vault_balance_samples("0xVAULT", start_ts=123, limit=5)
    sql_c, params_c = cur_c.exec_calls[0]
    assert "WHERE vault = %s AND timestamp >= %s" in sql_c and "LIMIT %s" in sql_c
    assert params_c == ("0xvault", 123, 5)
    assert out_c == list(reversed(rows_c))

    rows_d = [(5, 50, 0, 0, 5.0)]
    cur_d = _SqlCur(fetchall_result=rows_d)
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _yield_cursor(cur_d)):
        out_d = vault_storage.list_crystal_vault_balance_samples("0xVAULT", start_ts=456, limit=0)
    sql_d, params_d = cur_d.exec_calls[0]
    assert "WHERE vault = %s AND timestamp >= %s" in sql_d and "LIMIT %s" not in sql_d
    assert params_d == ("0xvault", 456)
    assert out_d == list(reversed(rows_d))


# average cost basis: deposits accumulate, withdrawals realize at avg entry
def test_avg_cost_from_flows_math():
    pos, entry, realized = vault_api._avg_cost_from_flows([(100, 1.0)])
    assert (pos, entry, realized) == (100.0, 1.0, 0.0)

    pos, entry, realized = vault_api._avg_cost_from_flows([(100, 1.0), (100, 2.0)])
    assert pos == 200.0
    assert abs(entry - 1.5) < 1e-9
    assert realized == 0.0

    pos, entry, realized = vault_api._avg_cost_from_flows([(100, 1.0), (100, 2.0), (-100, 3.0)])
    assert pos == 100.0
    assert abs(entry - 1.5) < 1e-9
    assert abs(realized - 150.0) < 1e-9

    pos, entry, realized = vault_api._avg_cost_from_flows([(-50, 1.0)])
    assert (pos, entry, realized) == (0.0, 0.0, 0.0)


# annualized vault apy comes from per-share growth over the sample window
def test_vault_apy_pct_from_samples():
    day = 86400
    now = 2_100_000_000
    rows = [
        (1, now - 2 * day, 0, 0, 100.0, 1000),
        (2, now - day, 0, 0, 101.0, 1000),
    ]
    with patch.object(vault_api.time, "time", return_value=float(now)):
        with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=rows):
            apy = vault_api._vault_apy_pct("0xv")
    assert apy is not None
    assert abs(apy - 1.0 * 365.0) < 1e-6

    with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=[rows[0]]):
        assert vault_api._vault_apy_pct("0xv") is None


# deposits that mint shares at nav must not read as vault pnl
def test_vault_history_per_share_pnl_ignores_flows():
    vault_row = _vault_row(vault="0xv")
    pts_rows = [
        (1, 1000, 0, 0, 100.0, 1000),
        (2, 1010, 0, 0, 200.0, 2000),
        (3, 1020, 0, 0, 220.0, 2000),
    ]
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row):
        with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=pts_rows):
            out = vault_api.vault_history("0xV", 4, limit=0)
    pnl = out["series"]["pnl"]
    assert pnl[0]["pnlPct"] == 0.0
    assert pnl[1]["pnlPct"] == 0.0
    assert abs(pnl[2]["pnlPct"] - 10.0) < 1e-9
    assert abs(pnl[2]["pnlUsd"] - 20.0) < 1e-9
    assert [p["tvlUsd"] for p in out["series"]["tvl"]] == [100.0, 200.0, 220.0]


# samples without shares are unpriceable and must report flat zero, never tvl deltas
def test_vault_history_legacy_rows_report_flat_pnl():
    vault_row = _vault_row(vault="0xv")
    pts_rows = [
        (1, 1000, 0, 0, 1.0),
        (2, 1010, 0, 0, 2.0),
    ]
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row):
        with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=pts_rows):
            out = vault_api.vault_history("0xV", 4, limit=0)
    pnl = out["series"]["pnl"]
    assert [p["pnlUsd"] for p in pnl] == [0.0, 0.0]
    assert [p["pnlPct"] for p in pnl] == [0.0, 0.0]


# a whale deposit at flat value per share must not move the dollar pnl series
def test_vault_history_dollar_series_is_flow_invariant():
    vault_row = _vault_row(vault="0xv")
    pts_rows = [
        (1, 1000, 0, 0, 100.0, 1000),
        (2, 1010, 0, 0, 110.0, 1000),
        (3, 1020, 0, 0, 110110.0, 1001000),
    ]
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row):
        with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=pts_rows):
            out = vault_api.vault_history("0xV", 4, limit=0)
    pnl = out["series"]["pnl"]
    assert abs(pnl[1]["pnlUsd"] - 10.0) < 1e-9
    assert abs(pnl[2]["pnlUsd"] - 10.0) < 1e-6
    assert abs(pnl[2]["pnlPct"] - 10.0) < 1e-9


# a zero usd sample carries the previous pnl forward instead of printing minus one hundred
def test_vault_history_zero_usd_sample_carries_forward():
    vault_row = _vault_row(vault="0xv")
    pts_rows = [
        (1, 1000, 0, 0, 100.0, 1000),
        (2, 1010, 0, 0, 0.0, 1000),
        (3, 1020, 0, 0, 110.0, 1000),
    ]
    with patch.object(vault_api.storage, "get_crystal_vault", return_value=vault_row):
        with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=pts_rows):
            out = vault_api.vault_history("0xV", 4, limit=0)
    pnl = out["series"]["pnl"]
    assert pnl[1]["pnlPct"] == 0.0
    assert pnl[1]["pnlUsd"] == 0.0
    assert abs(pnl[2]["pnlPct"] - 10.0) < 1e-9


# an exact full exit with 1e18 scale share counts leaves no float residue
def test_avg_cost_full_exit_is_exact():
    a = 4539448776366754703
    b = 1190396360377453094
    pos, entry, realized = vault_api._avg_cost_from_flows([(a, 1.0), (b, 2.0), (-(a + b), 3.0)])
    assert pos == 0
    assert entry == 0.0
    assert realized > 0.0


# snapshot pct change also normalizes by shares when they exist
def test_vault_snapshot_pct_change_is_per_share():
    rows = [
        (1, 1000, 0, 0, 100.0, 1000),
        (2, 1010, 0, 0, 200.0, 2000),
    ]
    with patch.object(vault_api.storage, "list_crystal_vault_balance_samples", return_value=rows):
        snap = vault_api._vault_snapshot_from_samples("0xv", timeframe=4)
    assert abs(snap["stats"]["pctChange"]) < 1e-9
    assert snap["stats"]["lastUsd"] == 200.0


if __name__ == "__main__":
    for fn in [
        test_avg_cost_from_flows_math,
        test_avg_cost_full_exit_is_exact,
        test_vault_apy_pct_from_samples,
        test_vault_history_per_share_pnl_ignores_flows,
        test_vault_history_legacy_rows_report_flat_pnl,
        test_vault_history_dollar_series_is_flow_invariant,
        test_vault_history_zero_usd_sample_carries_forward,
        test_vault_snapshot_pct_change_is_per_share,
        test_vaults_module_helpers,
        test_parse_vault_created_with_metadata,
        test_parse_vault_created_without_metadata_and_with_bad_metadata_hex,
        test_parse_vault_deposit_withdraw_and_status_events_and_admin_changes,
        test_parse_market_created_dynamic_offsets_decode_correctly,
        test_vault_snapshot_from_samples_timeframes_and_raw_default_behavior,
        test_vault_snapshot_from_samples_optional_downsampling_and_empty_cases,
        test_vault_snapshot_from_samples_pct_when_first_value_zero,
        test_vault_user_lockup_fields_helper,
        test_vault_user_summary_not_found_and_fallback_snapshot,
        test_vault_user_summary_handles_missing_latest_balance_row,
        test_vault_user_summary_full_path_maps_lists_and_computes_share_balances,
        test_vaults_list_invalid_status_and_empty_page,
        test_vaults_list_custom_sort_and_order_are_forwarded,
        test_vaults_list_maps_rows_and_snapshots,
        test_vaults_list_snapshot_fallback_and_disable_snapshot,
        test_vault_refresh_balance_not_found_and_rpc_failure,
        test_vault_refresh_balance_success_and_user_projection,
        test_vault_refresh_balance_handles_bad_head_and_bad_call_and_fallback_timestamp,
        test_vault_refresh_balance_sample_write_paths_without_latest_row,
        test_vault_rpc_jsonrpc_sync_invalid_response_branches,
        test_vault_timeframe_parser_accepts_aliases_and_rejects_bad_values,
        test_vault_history_not_found_invalid_timeframe_and_empty_series,
        test_vault_history_accepts_string_timeframes,
        test_vault_history_timeframe_calls_and_series_math,
        test_vault_storage_upsert_vault_sql_and_cursor_paths,
        test_vault_storage_update_fields_sql_and_cursor_paths,
        test_vault_storage_event_and_user_write_helpers_sql_and_cursor_paths,
        test_vault_storage_read_helpers_issue_expected_queries,
        test_vault_storage_list_vaults_page_builds_sql_for_filters_and_sorts,
        test_vault_storage_list_balance_samples_query_variants_and_ordering,
    ]:
        fn()
    print("Vault tests passed")
