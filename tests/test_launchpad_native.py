import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# api.api and api.routes.launchpad are mutually importing; api.api must be
# initialized first or importing the route module standalone raises ImportError.
import api.api  # noqa: F401
import state
from core import chain as h
from modules import launchpad as lp_mod

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
    stub = MagicMock()
    # a bare MagicMock is truthy, which would make every log look like a
    # duplicate and short-circuit the trade handler
    stub.trade_exists.return_value = False
    monkeypatch.setattr(state, "storage", stub)
    # keep tests off the network: pin the deployed launchpadInitialNativeSupply
    state._LAUNCHPAD_PARAMS_CACHE["initial_native_supply"] = V0
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

def test_trade_for_unknown_token_is_recovered_not_dropped(monkeypatch):
    st = _fresh_state(monkeypatch)
    monkeypatch.setattr(state, "_fetch_token_string", lambda tok, sel: "RECOVERED")

    # no TokenCreated was ever applied for TOKEN
    assert TOKEN not in st.launchpad_tokens

    nr = 2500 * 10 ** 18
    _buy(st, nr, blk=101, ts=1001)

    lp = st.launchpad_tokens.get(TOKEN)
    assert lp is not None, "a trade for an unseen token must not be dropped silently"
    assert lp.source == 0
    assert lp.quote_token == state.WMON
    assert lp.last_price_native == Decimal(nr) / Decimal(_reserves_after(nr))


def test_recovered_stub_is_backfilled_by_later_token_created(monkeypatch):
    st = _fresh_state(monkeypatch)
    monkeypatch.setattr(state, "_fetch_token_string", lambda tok, sel: "")

    _buy(st, 2500 * 10 ** 18, blk=101, ts=1001)
    assert st.launchpad_tokens[TOKEN].creator == ""

    _create_token(st, blk=102, ts=1002)
    lp = st.launchpad_tokens[TOKEN]
    assert lp.creator == CREATOR
    assert lp.name == "Test Token"
    assert lp.symbol == "TEST"


def test_zero_reserve_price_falls_back_to_amount_ratio(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)

    amount_in, amount_out = 10 ** 18, 4 * 10 ** 20
    parsed = lp_mod.parse_launchpad_trade(
        ROUTER,
        ["0x", _topic_addr(TOKEN), _topic_addr(USER)],
        _launchpad_trade_data(True, amount_in, amount_out, 0, 10 ** 27),
    )
    st.apply_launchpad_trade(parsed, 101, 1001, "0xdead", 0, ROUTER)

    lp = st.launchpad_tokens[TOKEN]
    assert lp.last_price_native == Decimal(amount_in) / Decimal(amount_out)
    assert lp.last_price_native > 0, "a zero reserve must not silently zero the price"


def test_circulating_supply_is_exact_and_matches_progress_bar(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)

    nr = 2500 * 10 ** 18
    _, tr = _buy(st, nr, blk=101, ts=1001)
    lp = st.launchpad_tokens[TOKEN]

    # exact integer, derived from the authoritative reserve
    assert lp.circulating_supply == 600_000_000
    assert isinstance(lp.circulating_supply, int)

    # the progress bar and the final-stretch flag agree at exactly 75%
    from api.routes.launchpad import _graduation_pct

    assert _graduation_pct(lp.circulating_supply, 0) == 0.75
    assert lp.approaching_75 is True


def test_circulating_supply_self_corrects_after_a_missed_trade(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)

    # a trade is missed entirely, then the next one arrives
    _buy(st, 2500 * 10 ** 18, blk=102, ts=1002)
    assert st.launchpad_tokens[TOKEN].circulating_supply == 600_000_000


def test_initial_price_tracks_launchpad_initial_native_supply(monkeypatch):
    for v0_mon in (1_000, 49_300, 141_600):
        st = _fresh_state(monkeypatch)
        # the adapter takes V0 by injection; that is the seam to patch
        monkeypatch.setattr(
            state.NATIVE_ADAPTER, "_initial_native_supply_fn", lambda v=v0_mon: v * 10 ** 18
        )
        _create_token(st)
        lp = st.launchpad_tokens[TOKEN]
        expected = Decimal(v0_mon * 10 ** 18) / Decimal(INITIAL_TOKEN_SUPPLY)
        assert lp.last_price_native == expected, v0_mon
    # the old hardcoded default is only right by coincidence at the test V0
    assert Decimal(1_000 * 10 ** 18) / Decimal(INITIAL_TOKEN_SUPPLY) == Decimal("0.000001")


def test_curve_reserves_are_recorded_for_fee_derivation(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)

    nr = 2500 * 10 ** 18
    _, tr = _buy(st, nr, blk=101, ts=1001)
    lp = st.launchpad_tokens[TOKEN]

    assert lp.curve_native_reserve == nr
    assert lp.curve_token_reserve == tr

    # a later trade overwrites them, so the delta is always vs the prior trade
    nr2 = 3000 * 10 ** 18
    _, tr2 = _buy(st, nr2, blk=102, ts=1002)
    assert lp.curve_native_reserve == nr2
    assert lp.curve_token_reserve == tr2


def test_fee_rate_is_derivable_from_the_native_reserve_delta(monkeypatch):
    """The contract credits the curve net of fee on a buy and reports gross in
    the event, so the keep-factor falls out of the reserve delta. No hardcoded
    rate, and a governance change shows up on the very next trade."""
    st = _fresh_state(monkeypatch)
    _create_token(st)

    keep_factor = 99_000  # launchpadFee: 1% fee
    v0 = V0
    k = v0 * INITIAL_TOKEN_SUPPLY

    prev_native = v0
    gross_in = 7 * 10 ** 18
    net_in = (gross_in * keep_factor) // 100_000
    new_native = prev_native + net_in
    new_token = (k + new_native - 1) // new_native

    _buy(st, prev_native, blk=101, ts=1001)  # establishes the previous reserve
    lp = st.launchpad_tokens[TOKEN]
    assert lp.curve_native_reserve == prev_native

    parsed = lp_mod.parse_launchpad_trade(
        ROUTER,
        ["0x", _topic_addr(TOKEN), _topic_addr(USER)],
        _launchpad_trade_data(True, gross_in, 1, new_native, new_token),
    )
    delta_native = int(parsed["native_reserve"]) - lp.curve_native_reserve
    implied_keep_factor = (delta_native * 100_000) // gross_in
    assert implied_keep_factor == keep_factor
    assert gross_in - delta_native == gross_in - net_in  # the fee actually taken

    st.apply_launchpad_trade(parsed, 102, 1002, "0xdead", 0, ROUTER)
    assert lp.curve_native_reserve == new_native


def test_pnl_unit_semantics_are_coherent():
    """Guard: balance_token is raw (1e18) and last_price_native is MON per WHOLE
    token, so balance_token * price already lands in native wei -- the same unit
    as realized_pnl_native. This looks like a 1e18 bug but is correct; dividing
    here would break it."""
    nr, tr = 2500 * 10 ** 18, 4 * 10 ** 26
    price = Decimal(nr) / Decimal(tr)

    whole_tokens = 500
    balance_raw = whole_tokens * 10 ** 18
    unrealized_wei = Decimal(balance_raw) * price
    assert unrealized_wei == Decimal(whole_tokens) * price * (10 ** 18)

    native_spent, native_received = 10 * 10 ** 18, 4 * 10 ** 18
    realized = Decimal(native_received - native_spent)
    total = realized + unrealized_wei
    # total PnL == (what you took out + what you still hold) - what you put in
    assert total == (Decimal(native_received) + unrealized_wei) - Decimal(native_spent)


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


def _transfer(st, frm, to, amount):
    parsed = h.PARSERS["TF"](TOKEN, ["0x", _topic_addr(frm), _topic_addr(to)], _word(amount))
    st.apply_token_transfer(parsed, 110, 1100, TOKEN)
    return parsed


def _balance_deltas(st):
    """(address, balance_token_delta) for every position write."""
    return [
        (c.kwargs["user_address"], c.kwargs["balance_token_delta"])
        for c in state.storage.upsert_position.call_args_list
    ]


def test_buy_transfer_credits_user_and_skips_contract_side(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)

    # a curve buy transfers tokens out of the Crystal core contract to the buyer
    _transfer(st, ROUTER, USER, 500 * 10 ** 18)

    deltas = _balance_deltas(st)
    assert (USER, 500 * 10 ** 18) in deltas
    assert all(addr != ROUTER for addr, _ in deltas), "contract side must not be tracked as a holder"


def test_sell_transfer_debits_user(monkeypatch):
    st = _fresh_state(monkeypatch)
    _create_token(st)

    _transfer(st, ROUTER, USER, 500 * 10 ** 18)
    _transfer(st, USER, ROUTER, 200 * 10 ** 18)

    deltas = _balance_deltas(st)
    assert (USER, 500 * 10 ** 18) in deltas
    assert (USER, -200 * 10 ** 18) in deltas
    assert sum(d for a, d in deltas if a == USER) == 300 * 10 ** 18


OTHER_TOKEN = "0xaaaabbbbccccddddeeeeffff0000111122223333"
OTHER_MARKET = "0x9999888877776666555544443333222211110000"


def _graduate(st):
    _create_token(st)
    _buy(st, 2500 * 10 ** 18, blk=101, ts=1001)
    st.apply_migrated(102, 1002, {"token": TOKEN}, ROUTER)
    st.apply_market_created(102, 1002, _market_created_ev(), ROUTER)
    return st.launchpad_tokens[TOKEN]


def _market_trade(st, market, is_buy, amount_in, amount_out, blk=103, ts=1003, log_idx=0):
    st.apply_market_trade(
        blk,
        ts,
        {
            "market": market,
            "user": USER,
            "is_buy": is_buy,
            "amount_in": amount_in,
            "amount_out": amount_out,
            "end_price": 20_000,
        },
        ROUTER,
        txh="0xfeed",
        log_idx=log_idx,
    )


def test_post_graduation_trades_keep_volume_flowing(monkeypatch):
    st = _fresh_state(monkeypatch)
    lp = _graduate(st)

    vol_before = lp.native_volume
    tx_before = lp.tx_count

    native_in, tokens_out = 3 * 10 ** 18, 150 * 10 ** 18
    _market_trade(st, MARKET, True, native_in, tokens_out)

    assert lp.native_volume == vol_before + native_in, "volume must not freeze at graduation"
    assert lp.token_volume >= tokens_out
    assert lp.tx_count == tx_before + 1
    assert lp.buy_count >= 1

    # and the trade is persisted as a launchpad trade, so windowed volume sees it
    tokens = [c.kwargs["token"] for c in state.storage.insert_trade.call_args_list]
    assert TOKEN in tokens


def test_post_graduation_sell_uses_correct_leg(monkeypatch):
    st = _fresh_state(monkeypatch)
    lp = _graduate(st)
    vol_before = lp.native_volume

    # sell: base in, quote (native) out
    tokens_in, native_out = 150 * 10 ** 18, 3 * 10 ** 18
    _market_trade(st, MARKET, False, tokens_in, native_out)

    assert lp.native_volume == vol_before + native_out
    assert lp.sell_count >= 1


def test_non_launchpad_market_trades_do_not_touch_launchpad_aggregates(monkeypatch):
    st = _fresh_state(monkeypatch)
    lp = _graduate(st)

    # an ordinary Crystal market, unrelated to any launchpad token
    other = dict(_market_created_ev())
    other.update(
        market=OTHER_MARKET,
        baseAsset=OTHER_TOKEN,
        baseAddress=OTHER_TOKEN,
        baseTicker="OTHER",
        marketId=2,
    )
    st.apply_market_created(103, 1003, other, ROUTER)

    assert OTHER_MARKET not in st.launchpad_market_to_token

    vol_before = lp.native_volume
    tx_before = lp.tx_count
    state.storage.insert_trade.reset_mock()

    _market_trade(st, OTHER_MARKET, True, 5 * 10 ** 18, 900 * 10 ** 18, log_idx=1)

    assert lp.native_volume == vol_before, "a non-launchpad market must not move launchpad volume"
    assert lp.tx_count == tx_before
    assert state.storage.insert_trade.call_count == 0


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
