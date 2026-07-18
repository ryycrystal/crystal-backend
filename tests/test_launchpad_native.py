import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain as h
from modules import launchpad as lp_mod
import state


ROUTER = h.CONTRACTS["ROUTER"].lower()
TOKEN = "0x1f5bb433d52b9e9219a4decb4e9abc87541c7777"
CREATOR = "0x77d4d8e13b228e474b1c53d6adeebef4dfa51603"
USER = "0x1234567890abcdef1234567890abcdef12345678"
MARKET = "0x975c4885538ba5072c66f48d4c4c7253e388c3e0"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"

INITIAL_TOKEN_SUPPLY = 10 ** 27
GRADUATED_TOKEN_SUPPLY = 2 * 10 ** 26
CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY - GRADUATED_TOKEN_SUPPLY
V0 = 1000 * 10 ** 18
K = V0 * INITIAL_TOKEN_SUPPLY


def _topic_addr(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _word(value: int) -> str:
    return f"{value:064x}"


def _string_tail(value: str) -> str:
    raw = value.encode()
    padded = ((len(raw) + 31) // 32) * 32
    return _word(len(raw)) + raw.hex().ljust(padded * 2, "0")


def _token_created_data(strings) -> str:
    tails = [_string_tail(s) for s in strings]
    offset = len(strings) * 32
    heads = []
    for tail in tails:
        heads.append(_word(offset))
        offset += len(tail) // 2
    return "".join(heads) + "".join(tails)


def _launchpad_trade_data(is_buy, amount_in, amount_out, native_reserve, token_reserve) -> str:
    return (
        _word(1 if is_buy else 0)
        + _word(amount_in)
        + _word(amount_out)
        + _word(native_reserve)
        + _word(token_reserve)
    )


def _reserves_after(native_reserve: int):
    return (K + native_reserve - 1) // native_reserve


def _fresh_state(monkeypatch):
    monkeypatch.setattr(state, "storage", MagicMock())
    st = state.State()
    st.tokenToPrice[WMON] = Decimal("0.02")
    st.mon_price_usd = Decimal("0.02")
    return st


def _create_token(st, blk=100, ts=1000):
    parsed = lp_mod.parse_token_created(
        ROUTER,
        ["0x", _topic_addr(TOKEN), _topic_addr(CREATOR)],
        _token_created_data(["Test Token", "TEST", "cid", "desc", "s1", "s2", "s3", "s4"]),
    )
    st.apply_token_created(blk, parsed, ts, ROUTER)
    return parsed


def _buy(st, native_reserve, blk, ts, amount_in=10 ** 18, amount_out=10 ** 20):
    token_reserve = _reserves_after(native_reserve)
    parsed = lp_mod.parse_launchpad_trade(
        ROUTER,
        ["0x", _topic_addr(TOKEN), _topic_addr(USER)],
        _launchpad_trade_data(True, amount_in, amount_out, native_reserve, token_reserve),
    )
    st.apply_launchpad_trade(parsed, blk, ts, "0xdead", 0, ROUTER)
    return parsed, token_reserve


# --- decoding matches the deployed contract ABI -------------------------------

def test_token_created_decodes_topics_and_strings():
    parsed = lp_mod.parse_token_created(
        ROUTER,
        ["0x", _topic_addr(TOKEN), _topic_addr(CREATOR)],
        _token_created_data(["Test Token", "TEST", "cid", "desc", "s1", "s2", "s3", "s4"]),
    )
    assert parsed["token"] == TOKEN
    assert parsed["creator"] == CREATOR
    assert parsed["name"] == "Test Token"
    assert parsed["symbol"] == "TEST"
    assert parsed["metadata_cid"] == "cid"
    assert parsed["social4"] == "s4"
    assert parsed["source"] == 0


def test_launchpad_trade_decodes_reserves_in_abi_order():
    nr = 2500 * 10 ** 18
    tr = _reserves_after(nr)
    parsed = lp_mod.parse_launchpad_trade(
        ROUTER,
        ["0x", _topic_addr(TOKEN), _topic_addr(USER)],
        _launchpad_trade_data(True, 111, 222, nr, tr),
    )
    assert parsed["token"] == TOKEN
    assert parsed["user"] == USER
    assert parsed["is_buy"] is True
    assert parsed["amount_in"] == 111
    assert parsed["amount_out"] == 222
    assert parsed["native_reserve"] == nr
    assert parsed["token_reserve"] == tr


def test_migrated_decodes_token_only():
    parsed = lp_mod.parse_migrated(ROUTER, ["0x", _topic_addr(TOKEN)], "")
    assert parsed["token"] == TOKEN


# --- curve state --------------------------------------------------------------

def test_trade_sets_price_from_reserves(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)
    nr = 2500 * 10 ** 18
    _, tr = _buy(st, nr, blk=101, ts=1001)
    lp = st.launchpad_tokens[TOKEN]
    assert lp.last_price_native == Decimal(nr) / Decimal(tr)


def test_final_stretch_fires_at_75pct_of_tokens_sold(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)

    # 333M sold -> below the 600M threshold
    _buy(st, 1500 * 10 ** 18, blk=101, ts=1001)
    lp = st.launchpad_tokens[TOKEN]
    sold_below = INITIAL_TOKEN_SUPPLY - _reserves_after(1500 * 10 ** 18)
    assert sold_below < (CURVE_SUPPLY * 3) // 4
    assert lp.approaching_75 is False

    # 600M sold -> exactly at the threshold
    _, tr = _buy(st, 2500 * 10 ** 18, blk=102, ts=1002)
    assert INITIAL_TOKEN_SUPPLY - tr == (CURVE_SUPPLY * 3) // 4 == 600_000_000 * 10 ** 18
    assert lp.approaching_75 is True
    assert lp.approaching_75_block == 102


def test_final_stretch_threshold_is_independent_of_initial_native_supply(monkeypatch):
    for v0_mon in (1_000, 49_300, 141_600):
        st = _fresh_state(monkeypatch)
        _create_token(st)
        v0 = v0_mon * 10 ** 18
        k = v0 * INITIAL_TOKEN_SUPPLY
        target_tr = INITIAL_TOKEN_SUPPLY - (CURVE_SUPPLY * 3) // 4
        nr = k // target_tr
        parsed = lp_mod.parse_launchpad_trade(
            ROUTER,
            ["0x", _topic_addr(TOKEN), _topic_addr(USER)],
            _launchpad_trade_data(True, 10 ** 18, 10 ** 20, nr, target_tr),
        )
        st.apply_launchpad_trade(parsed, 101, 1001, "0xdead", 0, ROUTER)
        assert st.launchpad_tokens[TOKEN].approaching_75 is True, v0_mon


# --- graduation ---------------------------------------------------------------

def _market_created_ev():
    return {
        "isCanonical": True,
        "marketType": 3,
        "market": MARKET,
        "quoteAsset": WMON,
        "baseAsset": TOKEN,
        "quoteAddress": WMON,
        "baseAddress": TOKEN,
        "quoteDecimals": 18,
        "baseDecimals": 18,
        "quoteTicker": "WMON",
        "baseTicker": "TEST",
        "quoteName": "Wrapped Monad",
        "baseName": "Test Token",
        "marketId": 1,
        "scaleFactor": 9,
        "tickSize": 1,
        "maxPrice": 10 ** 15,
        "minSize": 1,
        "takerFee": 99910,
        "makerRebate": 99995,
    }


def test_graduation_links_market_to_launchpad_token(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)
    _buy(st, 2500 * 10 ** 18, blk=101, ts=1001)

    st.apply_migrated(102, 1002, {"token": TOKEN}, ROUTER)
    lp = st.launchpad_tokens[TOKEN]
    assert lp.migrated is True

    st.apply_market_created(102, 1002, _market_created_ev(), ROUTER)
    assert lp.market == MARKET, "graduated market must be linked to the launchpad token"
    assert st.launchpad_market_to_token[MARKET] == TOKEN


def test_post_graduation_market_trade_updates_token_price(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)
    _buy(st, 2500 * 10 ** 18, blk=101, ts=1001)
    st.apply_migrated(102, 1002, {"token": TOKEN}, ROUTER)
    st.apply_market_created(102, 1002, _market_created_ev(), ROUTER)

    lp = st.launchpad_tokens[TOKEN]
    frozen = lp.last_price_native

    # scaleFactor 9 -> price = end_price / 1e9
    end_price = 20_000
    st.apply_market_trade(
        103,
        1003,
        {"market": MARKET, "user": USER, "is_buy": True, "amount_in": 1, "amount_out": 1, "end_price": end_price},
        ROUTER,
    )

    assert lp.last_price_native == Decimal(end_price) / (Decimal(10) ** 9)
    assert lp.last_price_native != frozen, "price must not freeze at the last bonding-curve tick"
