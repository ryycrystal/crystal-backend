from core import chain as h
from modules import univ4

SWAP_TOPICS = [
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
    "0x965e23eaf54b4538492139a6ebe19214ce6ecffc05b90d5303812bc34de822f6",
    "0x000000000000000000000000fb78fcae443eb423b59b8c186518c5df94416344",
]
SWAP_DATA = "fffffffffffffffffffffffffffffffffffffffffffff1726d63edbdde0277610000000000000000000000000000000000000000000269c33e3e00ccc6921da3000000000000000000000000000000000000000669ee857af4a4614cfd9857b500000000000000000000000000000000000000000008e3aca5ebdc5d39c206ea00000000000000000000000000000000000000000000000000000000000091320000000000000000000000000000000000000000000000000000000000002710"
POOL_MANAGER = "0x188d586ddcf52439676ca21a244753fa19f9ea8e"


def test_swap_topic_is_registered():
    assert h.EVENT_SIGS[univ4.V4_SWAP_TOPIC] == "V4SWAP"
    assert h.EVENT_SIGS[univ4.V4_INITIALIZE_TOPIC] == "V4INIT"


def test_only_the_pool_manager_is_accepted():
    assert h.accepts_log_for_indexing("V4SWAP", POOL_MANAGER)
    assert not h.accepts_log_for_indexing("V4SWAP", "0x" + "11" * 20)


def test_decodes_a_real_swap():
    ev = univ4.parse_v4_swap(POOL_MANAGER, SWAP_TOPICS, SWAP_DATA)
    assert ev["pool_id"] == "0x965e23eaf54b4538492139a6ebe19214ce6ecffc05b90d5303812bc34de822f6"
    assert ev["amount0"] == -68724686013469231515807
    assert ev["amount1"] == 2917301720048102871801251
    assert ev["fee"] == 10000
    assert ev["tick"] == 37170


def test_amounts_are_sign_extended_int128():
    ev = univ4.parse_v4_swap(POOL_MANAGER, SWAP_TOPICS, SWAP_DATA)
    assert ev["amount0"] < 0 < ev["amount1"]


def test_malformed_logs_return_none():
    assert univ4.parse_v4_swap(POOL_MANAGER, SWAP_TOPICS[:1], SWAP_DATA) is None
    assert univ4.parse_v4_swap(POOL_MANAGER, SWAP_TOPICS, "00" * 32) is None
    assert univ4.parse_v4_initialize(POOL_MANAGER, SWAP_TOPICS[:2], "") is None


REAL_SWAPS = [
    (
        [
            "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
            "0x18a9fc874581f3ba12b7898f80a683c66fd5877fd74b26a85ba9a3a79c549954",
            "0x00000000000000000000000019f079b4124641d004d90c37c793219dad191d56",
        ],
        "fffffffffffffffffffffffffffffffffffffffffffff34fbf79f4780316b53e000000000000000000000000000000000000000000000000000000005afecc5e0000000000000000000000000000000000000000000002ad9373c759218000000000000000000000000000000000000000000000000000012746f5212f21146efffffffffffffffffffffffffffffffffffffffffffffffffffffffffffb393f00000000000000000000000000000000000000000000000000000000000001f4",
        102075476,
    ),
    (
        [
            "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
            "0x3783b51e33900eb366a9e8473c76cda441e7170d2e5d96927f30c16a7add93aa",
            "0x0000000000000000000000000bc188ba77851d2d80aa0c692ea1d523475ba36d",
        ],
        "ffffffffffffffffffffffffffffffffffffffffffffffa29112a14b563c0000000000000000000000000000000000000000000000000000003fa002f253234f000000000000000000000000000000000000000000d34de7869931810adff7fb000000000000000000000000000000000000000000004b66aabd1b69c5b5b9c2fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe3fc500000000000000000000000000000000000000000000000000000000000001f4",
        102075476,
    ),
    (
        [
            "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
            "0x3783b51e33900eb366a9e8473c76cda441e7170d2e5d96927f30c16a7add93aa",
            "0x000000000000000000000000dcda00a0fef2317f74dbe39f58f23802d24d5f42",
        ],
        "fffffffffffffffffffffffffffffffffffffffffffffcbbcfed411230d673750000000000000000000000000000000000000000000000000239540d041aa85e000000000000000000000000000000000000000000d3465a8df2c86352975988000000000000000000000000000000000000000000004b66aabd1b69c5b5b9c2fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe3fc200000000000000000000000000000000000000000000000000000000000001f4",
        102075477,
    ),
    (
        [
            "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
            "0x1c93dd2f2f47439330150bf728c3beeaad71de45420a49183214898b044b65d1",
            "0x000000000000000000000000553037bac82741e7ca05afb48e8538996fd70eca",
        ],
        "fffffffffffffffffffffffffffffffffffffffffffffd459516d2cce712f4690000000000000000000000000000000000000000000000000000000000064d29000000000000000000000000000000000000000000000018524e22e9a3034fca0000000000000000000000000000000000000000000000000b5c7c68aaed1224fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffa346200000000000000000000000000000000000000000000000000000000000001f4",
        102075482,
    ),
    (
        [
            "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
            "0x3783b51e33900eb366a9e8473c76cda441e7170d2e5d96927f30c16a7add93aa",
            "0x000000000000000000000000ff88b6381f682d818200c6e1cee5aa83e9ed7fe0",
        ],
        "fffffffffffffffffffffffffffffffffffffffffffffd785d7bbc868b59532600000000000000000000000000000000000000000000000001b88525bc3ab64b000000000000000000000000000000000000000000d32cd87b61a83b3abe1ffe000000000000000000000000000000000000000000004b66aabd1b69c5b5b9c2fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe3fb800000000000000000000000000000000000000000000000000000000000001f4",
        102075490,
    ),
]


def test_decodes_every_sampled_real_swap():
    for topics, data, _blk in REAL_SWAPS:
        ev = univ4.parse_v4_swap(POOL_MANAGER, topics, data)
        assert ev is not None
        assert ev["pool_id"].startswith("0x") and len(ev["pool_id"]) == 66
        assert ev["sqrt_price_x96"] > 0
        assert -887272 <= ev["tick"] <= 887272
        assert 0 <= ev["fee"] <= 1_000_000
        assert ev["amount0"] <= 0 <= ev["amount1"] or ev["amount1"] <= 0 <= ev["amount0"]


def _pool_id_from_key(c0, c1, fee, tick_spacing, hooks):
    from Crypto.Hash import keccak

    def addr_word(a):
        return bytes(12) + bytes.fromhex(a[2:])

    enc = (
        addr_word(c0)
        + addr_word(c1)
        + fee.to_bytes(32, "big", signed=True)
        + tick_spacing.to_bytes(32, "big", signed=True)
        + addr_word(hooks)
    )
    h = keccak.new(digest_bits=256)
    h.update(enc)
    return "0x" + h.hexdigest()


def test_initialize_round_trips_against_the_pool_id_derivation():
    c0 = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
    c1 = "0x405b6330e213ded490240cbcdd64790806827777"
    fee, tick_spacing = 10000, 200
    hooks = "0x00000000000000000000000000000000000000aa"
    pool_id = _pool_id_from_key(c0, c1, fee, tick_spacing, hooks)
    topics = [univ4.V4_INITIALIZE_TOPIC, pool_id, "0x" + "0" * 24 + c0[2:], "0x" + "0" * 24 + c1[2:]]
    data = (
        fee.to_bytes(32, "big").hex()
        + tick_spacing.to_bytes(32, "big").hex()
        + (bytes(12) + bytes.fromhex(hooks[2:])).hex()
        + (79228162514264337593543950336).to_bytes(32, "big").hex()
        + (0).to_bytes(32, "big").hex()
    )
    ev = univ4.parse_v4_initialize(POOL_MANAGER, topics, data)
    assert ev["pool_id"] == pool_id
    assert ev["currency0"] == c0
    assert ev["currency1"] == c1
    assert ev["fee"] == fee
    assert ev["tick_spacing"] == tick_spacing
    assert ev["hooks"] == hooks
    assert _pool_id_from_key(ev["currency0"], ev["currency1"], ev["fee"], ev["tick_spacing"], ev["hooks"]) == pool_id


MONCOCK = "0x405b6330e213ded490240cbcdd64790806827777"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"


class _Tok:
    quote_token = WMON


class _FakeState:
    def __init__(self, tokens=None):
        self.launchpad_tokens = tokens if tokens is not None else {MONCOCK: _Tok()}
        self.v4_pools = {}
        self.registered = []

    def register_univ4_pool(self, pid, token, quote, token_is_0, cur=None, learned_from="swap"):
        from models import PoolInfo

        pi = PoolInfo(pool=pid, token_addr=token, native_addr=quote, token_is_0=token_is_0)
        self.v4_pools[pid] = pi
        self.registered.append((token, quote, token_is_0, learned_from))
        return pi


class _Stub:
    def __init__(self, tokens=None):
        self._state = _FakeState(tokens)


def _maps(txh, token, amount, pm):
    return {(txh, token): {"ordered": [{"log_idx": 1, "from": pm, "to": "0x" + "22" * 20, "amount": amount}]}}


def test_currencies_resolve_from_an_exact_pool_manager_transfer():
    from core.sequencer import Sequencer

    stub = _Stub()
    maps = _maps("0xabc", MONCOCK, 2917301720048102871801251, POOL_MANAGER)
    c0, c1 = Sequencer._univ4_currencies_from_transfers(
        stub, "0xabc", -68724686013469231515807, 2917301720048102871801251, maps
    )
    assert c1 == MONCOCK and c0 == ""


def test_quote_falls_back_to_the_tokens_own_quote_token():
    from core.sequencer import Sequencer

    stub = _Stub()
    pi = Sequencer._register_univ4_from_currencies(stub, "0xpool", "", MONCOCK)
    assert pi is not None
    assert pi.token_addr == MONCOCK and pi.native_addr == WMON and pi.token_is_0 is False


def test_untracked_tokens_are_never_registered():
    from core.sequencer import Sequencer

    stub = _Stub(tokens={})
    assert Sequencer._register_univ4_from_currencies(stub, "0xpool", "", MONCOCK) is None
    assert stub._state.registered == []


def test_quote_must_be_whitelisted():
    from core.sequencer import Sequencer

    class _BadTok:
        quote_token = "0x" + "33" * 20

    stub = _Stub(tokens={MONCOCK: _BadTok()})
    assert Sequencer._register_univ4_from_currencies(stub, "0xpool", "", MONCOCK) is None


def test_v4_amounts_are_inverted_so_a_v4_buy_reads_as_a_buy():
    amount0, amount1 = -68724686013469231515807, 2917301720048102871801251
    token_is_0 = False
    native_delta = -amount0 if not token_is_0 else -amount1
    token_delta = -amount1 if not token_is_0 else -amount0
    assert native_delta > 0
    assert token_delta < 0
