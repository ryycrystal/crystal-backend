import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain
from modules import nadfun
import state


V2_BONDING = "0x9f3832732923252A21044F21eE6bd87F09514ae4"
CREATOR = "0x77D4d8E13b228e474b1c53D6adEeBEf4DFA51603"
TOKEN = "0x1F5Bb433D52B9e9219a4DECB4e9AbC87541c7777"
POOL = "0x697be25fe455c09b1aa6fccba95a028bad57ba5c"
QUOTE = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"
LVMON = "0x91b81bfbe3A747230F0529Aa28d8b2Bc898E6D56"


def _topic_addr(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _word(value: int) -> str:
    return f"{value:064x}"


def _addr_word(addr: str) -> str:
    return addr.lower().removeprefix("0x").rjust(64, "0")


def _string_tail(value: str) -> str:
    raw = value.encode()
    padded_len = ((len(raw) + 31) // 32) * 32
    return _word(len(raw)) + raw.hex().ljust(padded_len * 2, "0")


def _v2_create_data(name: str, symbol: str, uri: str) -> str:
    tails = [_string_tail(name), _string_tail(symbol), _string_tail(uri)]
    head_words = 7
    offset = head_words * 32
    offsets = []
    for tail in tails:
        offsets.append(offset)
        offset += len(tail) // 2

    head = [
        _addr_word(QUOTE),
        _word(offsets[0]),
        _word(offsets[1]),
        _word(offsets[2]),
        _word(123),
        _word(456),
        _word(789),
    ]
    return "".join(head + tails)


def test_v2_nadfun_address_is_indexed():
    assert V2_BONDING.lower() in chain.NADFUN_ADDRS
    assert V2_BONDING.lower() in chain.ADDRS
    assert chain.EVENT_SIGS[nadfun.V2_CREATE_TOPIC] == "NFC"
    assert chain.accepts_log_for_indexing("NFC", V2_BONDING)
    assert chain.EVENT_SIGS[nadfun.V2_SNIPING_PENALTY_TOPIC] == "NFPEN"
    assert chain.accepts_log_for_indexing("NFPEN", V2_BONDING)


def test_parse_v2_create_event_uses_shifted_string_offsets():
    topics = [
        nadfun.V2_CREATE_TOPIC,
        _topic_addr(CREATOR),
        _topic_addr(TOKEN),
        _topic_addr(POOL),
    ]

    parsed = nadfun.parse_nadfun_token_created(
        V2_BONDING,
        topics,
        _v2_create_data("Touchgrass", "GRASS", ""),
    )

    assert parsed["creator"] == CREATOR.lower()
    assert parsed["token"] == TOKEN.lower()
    assert parsed["pool"] == POOL.lower()
    assert parsed["quote_token"] == QUOTE.lower()
    assert parsed["name"] == "Touchgrass"
    assert parsed["symbol"] == "GRASS"
    assert parsed["source"] == 1


def test_parse_v2_buy_event_uses_token_then_user_topic_order():
    topics = [
        nadfun.V2_BUY_TOPIC,
        _topic_addr(TOKEN),
        _topic_addr(CREATOR),
    ]
    data = _word(50) + _word(100)

    parsed = nadfun.parse_nadfun_buy(V2_BONDING, topics, data)

    assert parsed["token"] == TOKEN.lower()
    assert parsed["user"] == CREATOR.lower()
    assert parsed["is_buy"] is True
    assert parsed["amount_in"] == 50
    assert parsed["amount_out"] == 100


def test_parse_v2_sniping_penalty_event():
    topics = [
        nadfun.V2_SNIPING_PENALTY_TOPIC,
        _topic_addr(TOKEN),
        _topic_addr(CREATOR),
    ]
    data = _word(12) + _word(345)

    parsed = nadfun.parse_nadfun_sniping_penalty(V2_BONDING, topics, data)

    assert parsed["token"] == TOKEN.lower()
    assert parsed["user"] == CREATOR.lower()
    assert parsed["sniping_fee"] == 12
    assert parsed["penalty_bps"] == 345


def test_v2_pair_swap_is_address_gated_and_decoded_as_pool_delta():
    pool = "0x697be25fe455c09b1aa6fccba95a028bad57ba5c"
    unrelated_pool = "0x1111111111111111111111111111111111111111"
    if pool.lower() not in chain.ADDRS:
        chain.ADDRS.append(pool.lower())

    assert chain.EVENT_SIGS[nadfun.V2_PAIR_SWAP_TOPIC] == "V2SWAP"
    assert chain.accepts_log_for_indexing("V2SWAP", pool)
    assert not chain.accepts_log_for_indexing("V2SWAP", unrelated_pool)

    topics = [
        nadfun.V2_PAIR_SWAP_TOPIC,
        _topic_addr(V2_BONDING),
        _topic_addr(CREATOR),
    ]
    data = _word(10) + _word(0) + _word(0) + _word(25)

    parsed = nadfun.parse_v2_pair_swap(pool, topics, data)

    assert parsed["pool"] == pool.lower()
    assert parsed["sender"] == V2_BONDING.lower()
    assert parsed["user"] == CREATOR.lower()
    assert parsed["amount0"] == 10
    assert parsed["amount1"] == -25
    assert parsed["sqrt_price_x96"] == 0


def test_v2_created_token_keeps_non_wmon_quote_in_state_and_storage(monkeypatch):
    captured_token = {}
    captured_pool = {}

    monkeypatch.setattr(
        state.storage,
        "upsert_token_created",
        lambda **kwargs: captured_token.update(kwargs),
    )
    monkeypatch.setattr(state.storage, "increment_user_tokens_created", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        state.storage,
        "upsert_pool",
        lambda **kwargs: captured_pool.update(kwargs),
    )

    st = state.State()
    ev = {
        "token": TOKEN,
        "creator": CREATOR,
        "name": "Touchgrass",
        "symbol": "GRASS",
        "source": 1,
        "pool": POOL,
        "quote_token": LVMON,
    }

    st.apply_token_created(100, ev, 1234, V2_BONDING)

    token = TOKEN.lower()
    quote = LVMON.lower()
    lp = st.launchpad_tokens[token]

    assert lp.quote_token == quote
    assert captured_token["quote_token"] == quote
    assert st.v3_pools[POOL.lower()].native_addr == quote
    assert captured_pool["native_addr"] == quote
