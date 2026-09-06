from decimal import Decimal

from core.ledger.netflow import net_transaction
from core.ledger.types import (
    BASIS_ESTIMATED,
    BASIS_OBSERVED,
    BASIS_UNRESOLVED,
    KIND_AIRDROP,
    KIND_BUY,
    KIND_CUSTODY_DEPOSIT,
    KIND_LP_ADD,
    KIND_MINT,
    KIND_SELL,
    KIND_SWAP_LEG,
    KIND_TRANSFER_IN,
    KIND_TRANSFER_OUT,
    LVMON,
    NATIVE,
    SOURCE_RECONCILE,
    SOURCE_TRACE,
    SOURCE_TRANSFER_NET,
    SOURCE_VENUE_EVENT,
    USDC,
    WMON,
    ZERO,
    Rates,
    TokenReg,
    TraceResult,
    TransferLeg,
    TxBundle,
    TxMeta,
    VenueEvent,
)

TOKEN = "0x" + "aa" * 20
TOKEN2 = "0x" + "ab" * 20
CORE = "0x" + "c0" * 20
SETTLER = "0x" + "5e" * 20
ROUTER = "0x" + "70" * 20
POOL = "0x" + "b0" * 20
POOL_MANAGER = "0x" + "b4" * 20
CUSTODY = "0x" + "cc" * 20
BATCHER = "0x" + "ba" * 20
BUNDLER = "0x" + "bd" * 20
ACCOUNT = "0x" + "4a" * 20
WALLET = "0x" + "11" * 20
WALLET2 = "0x" + "22" * 20
WALLET3 = "0x" + "33" * 20
SEVEN = "0x" + "77" * 20
E18 = 10**18

KINDS = {
    CORE: "venue_curve",
    SETTLER: "venue_router",
    ROUTER: "venue_router",
    POOL: "venue_pool",
    POOL_MANAGER: "venue_pool",
    CUSTODY: "venue_custody",
    BATCHER: "venue_router",
    BUNDLER: "eoa",
    ACCOUNT: "wallet_4337",
    SEVEN: "eoa_7702",
    TOKEN: "token",
    TOKEN2: "token",
    WMON: "token",
    LVMON: "token",
    USDC: "token",
}


def kind_of(addr: str) -> str:
    return KINDS.get(addr, "eoa")


REGISTRY = {
    TOKEN: TokenReg(token=TOKEN, source="crystal", registered_block=1),
    TOKEN2: TokenReg(token=TOKEN2, source="crystal", registered_block=1),
}


def tf(idx: int, token: str, src: str, dst: str, amount: int) -> TransferLeg:
    return TransferLeg(log_index=idx, token=token, from_addr=src, to_addr=dst, amount=amount)


def lt(
    idx: int, user: str, is_buy: bool, native: int, tokens: int, address: str = CORE, token: str = TOKEN
) -> VenueEvent:
    parsed = {
        "token": token,
        "user": user,
        "is_buy": is_buy,
        "amount_in": native if is_buy else tokens,
        "amount_out": tokens if is_buy else native,
        "native_reserve": 0,
        "token_reserve": 0,
    }
    return VenueEvent(tag="LT", log_index=idx, parsed=parsed, address=address)


def meta(src: str, dst: str, value: int = 0) -> TxMeta:
    return TxMeta(txhash="0xtx", block_number=100, tx_index=3, from_addr=src, to_addr=dst, value=value, selector="0x")


def bundle(transfers, events=(), tx_meta=None, trace=None, userop_sender=None) -> TxBundle:
    return TxBundle(
        txhash="0xtx",
        block_number=100,
        tx_index=3,
        timestamp=1_700_000_000,
        transfers=list(transfers),
        venue_events=list(events),
        meta=tx_meta,
        trace=trace,
        userop_sender=userop_sender,
    )


def run(b: TxBundle, **kw):
    return net_transaction(b, REGISTRY, kind_of, **kw)


def only(flows, wallet=WALLET, token=TOKEN):
    matches = [f for f in flows if f.wallet == wallet and f.token == token]
    assert len(matches) == 1, flows
    return matches[0]


def test_curve_buy_native_via_tx_value():
    tokens, native = 1_000 * E18, 5 * E18
    b = bundle([tf(7, TOKEN, CORE, WALLET, tokens)], [lt(8, WALLET, True, native, tokens)], meta(WALLET, CORE, native))
    flows = run(b, rates=Rates(mon_usd=Decimal(2)))
    assert [f.wallet for f in flows] == [WALLET]
    f = flows[0]
    assert f.kind == KIND_BUY
    assert f.token_delta == tokens
    assert f.quote_asset == NATIVE
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_TRANSFER_NET
    assert f.venue == CORE
    assert f.counterparty == CORE
    assert f.origin == WALLET
    assert f.price_native == Decimal(native) / Decimal(tokens)
    assert f.mon_value == Decimal(5)
    assert f.usd_value == Decimal(10)
    assert f.log_index == 7
    assert f.sub_index == 0


def test_curve_sell_via_settler_amount_match():
    tokens, native = 162_232_261 * E18, 6_968 * E18
    b = bundle(
        [tf(3, TOKEN, WALLET, SETTLER, tokens), tf(4, TOKEN, SETTLER, CORE, tokens)],
        [lt(5, SETTLER, False, native, tokens)],
        meta(WALLET, SETTLER, 0),
    )
    flows = run(b)
    assert [f.wallet for f in flows] == [WALLET]
    f = flows[0]
    assert f.kind == KIND_SELL
    assert f.token_delta == -tokens
    assert f.quote_asset == NATIVE
    assert f.quote_delta == native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.counterparty == SETTLER
    assert f.venue == CORE
    assert f.log_index == 3


def test_routed_buy_via_router_names_router():
    tokens, native = 500 * E18, 3 * E18
    b = bundle(
        [tf(2, TOKEN, CORE, ROUTER, tokens), tf(3, TOKEN, ROUTER, WALLET, tokens)],
        [lt(4, ROUTER, True, native, tokens)],
        meta(WALLET, ROUTER, native),
    )
    flows = run(b)
    f = only(flows)
    assert len(flows) == 1
    assert f.kind == KIND_BUY
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_TRANSFER_NET
    assert f.counterparty == ROUTER
    assert f.venue == CORE


def test_pool_swap_with_wmon_is_observed_and_pool_gets_no_row():
    tokens, wmon = 400 * E18, 2 * E18
    swap = VenueEvent(
        tag="V3SWAP",
        log_index=12,
        parsed={"pool": POOL, "sender": WALLET, "user": WALLET, "amount0": -tokens, "amount1": wmon},
        address=POOL,
    )
    b = bundle([tf(10, WMON, WALLET, POOL, wmon), tf(11, TOKEN, POOL, WALLET, tokens)], [swap], meta(WALLET, POOL))
    flows = run(b)
    assert [f.wallet for f in flows] == [WALLET]
    f = flows[0]
    assert f.kind == KIND_BUY
    assert f.quote_asset == WMON
    assert f.quote_delta == -wmon
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_TRANSFER_NET
    assert f.venue == POOL
    assert f.price_native == Decimal(wmon) / Decimal(tokens)


def test_pool_sell_unwrapped_through_router_resolves_from_swap_event():
    tokens, wmon = 400 * E18, 2 * E18
    swap = VenueEvent(
        tag="V3SWAP",
        log_index=12,
        parsed={"pool": POOL, "sender": ROUTER, "user": ROUTER, "amount0": tokens, "amount1": -wmon},
        address=POOL,
    )
    b = bundle(
        [tf(10, TOKEN, WALLET, ROUTER, tokens), tf(11, TOKEN, ROUTER, POOL, tokens), tf(13, WMON, POOL, ROUTER, wmon)],
        [swap],
        meta(WALLET, ROUTER),
    )
    f = only(run(b))
    assert f.kind == KIND_SELL
    assert f.quote_asset == WMON
    assert f.quote_delta == wmon
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.venue == POOL


def test_v4_native_pool_uses_inverted_sign_convention():
    tokens, native = 300 * E18, 7 * E18
    swap = VenueEvent(
        tag="V4SWAP",
        log_index=5,
        parsed={"pool_id": "0x01", "sender": ROUTER, "amount0": tokens, "amount1": -native},
        address=POOL_MANAGER,
    )
    b = bundle(
        [tf(4, TOKEN, POOL_MANAGER, ROUTER, tokens), tf(6, TOKEN, ROUTER, WALLET, tokens)], [swap], meta(WALLET, ROUTER)
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.quote_asset == NATIVE
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.venue == POOL_MANAGER


def test_multi_wallet_batch_is_pro_rata_estimated():
    total, native = 1_000 * E18, 100 * E18
    b = bundle(
        [
            tf(1, TOKEN, CORE, BATCHER, total),
            tf(2, TOKEN, BATCHER, WALLET, 500 * E18),
            tf(3, TOKEN, BATCHER, WALLET2, 300 * E18),
            tf(4, TOKEN, BATCHER, WALLET3, 200 * E18),
        ],
        [lt(5, BATCHER, True, native, total)],
        meta(WALLET, BATCHER, native),
    )
    flows = run(b)
    assert [f.wallet for f in flows] == sorted([WALLET, WALLET2, WALLET3])
    by_wallet = {f.wallet: f for f in flows}
    assert by_wallet[WALLET].quote_delta == -native
    assert by_wallet[WALLET].basis_state == BASIS_OBSERVED
    assert by_wallet[WALLET].source == SOURCE_TRANSFER_NET
    for w, share in ((WALLET2, 300), (WALLET3, 200)):
        f = by_wallet[w]
        assert f.kind == KIND_BUY
        assert f.basis_state == BASIS_ESTIMATED
        assert f.source == SOURCE_VENUE_EVENT
        assert f.quote_delta == -(native * share // 500)
        assert f.price_native == Decimal(native) / Decimal(total)
        assert f.venue == CORE
    assert [f.sub_index for f in flows] == [0, 1, 2]


def test_pro_rata_remainder_sums_exactly():
    total = 3 * E18
    native = 10**18 + 1
    b = bundle(
        [
            tf(1, TOKEN, CORE, BATCHER, total),
            tf(2, TOKEN, BATCHER, WALLET2, E18),
            tf(3, TOKEN, BATCHER, WALLET3, E18),
            tf(4, TOKEN, BATCHER, SEVEN, E18),
        ],
        [lt(5, BATCHER, True, native, total)],
        meta(BUNDLER, BATCHER, native),
    )
    flows = run(b)
    assert all(f.basis_state == BASIS_ESTIMATED for f in flows)
    assert sum(f.quote_delta for f in flows) == -native


def test_plain_transfer_between_wallets():
    amount = 42 * E18
    flows = run(bundle([tf(9, TOKEN, WALLET, WALLET2, amount)], tx_meta=meta(WALLET, TOKEN)))
    assert [f.wallet for f in flows] == sorted([WALLET, WALLET2])
    out = only(flows, WALLET)
    inn = only(flows, WALLET2)
    assert out.kind == KIND_TRANSFER_OUT
    assert out.token_delta == -amount
    assert out.basis_state == BASIS_OBSERVED
    assert out.counterparty == WALLET2
    assert out.venue is None
    assert out.quote_asset is None and out.quote_delta is None
    assert inn.kind == KIND_TRANSFER_IN
    assert inn.token_delta == amount
    assert inn.basis_state == BASIS_UNRESOLVED
    assert inn.counterparty == WALLET
    assert inn.price_native is None
    assert inn.mon_value == 0


def test_airdrop_from_zero_and_creator_mint():
    amount = 10 * E18
    flows = run(bundle([tf(1, TOKEN, ZERO, WALLET2, amount)], tx_meta=meta(WALLET, TOKEN)))
    f = only(flows, WALLET2)
    assert f.kind == KIND_AIRDROP
    assert f.basis_state == BASIS_UNRESOLVED
    assert f.counterparty == ZERO
    flows = run(bundle([tf(1, TOKEN, ZERO, WALLET, amount)], tx_meta=meta(WALLET, TOKEN)))
    assert only(flows).kind == KIND_MINT
    assert only(flows).basis_state == BASIS_UNRESOLVED


def test_custody_deposit_to_crystal():
    amount = 5 * E18
    f = only(run(bundle([tf(1, TOKEN, WALLET, CUSTODY, amount)], tx_meta=meta(WALLET, CUSTODY))))
    assert f.kind == KIND_CUSTODY_DEPOSIT
    assert f.token_delta == -amount
    assert f.basis_state == BASIS_OBSERVED
    assert f.venue == CUSTODY
    assert f.quote_delta is None


def test_token_to_token_swap_two_swap_legs():
    a_out, b_in = 100 * E18, 50 * E18
    b = bundle(
        [
            tf(1, TOKEN, WALLET, ROUTER, a_out),
            tf(2, TOKEN, ROUTER, POOL, a_out),
            tf(3, TOKEN2, POOL, ROUTER, b_in),
            tf(4, TOKEN2, ROUTER, WALLET, b_in),
        ],
        tx_meta=meta(WALLET, ROUTER),
    )
    prices = {TOKEN: Decimal("0.5"), TOKEN2: Decimal(1)}
    flows = run(b, reference_price=prices.get)
    assert len(flows) == 2
    a = only(flows, WALLET, TOKEN)
    bb = only(flows, WALLET, TOKEN2)
    assert a.kind == KIND_SWAP_LEG and bb.kind == KIND_SWAP_LEG
    assert a.basis_state == BASIS_ESTIMATED and bb.basis_state == BASIS_ESTIMATED
    assert a.source == SOURCE_RECONCILE
    assert a.quote_delta == a_out // 2
    assert bb.quote_delta == -b_in
    assert a.mon_value == Decimal(50)
    assert bb.mon_value == Decimal(50)
    assert a.price_native == Decimal("0.5")
    assert a.venue == POOL and bb.venue == POOL
    assert (a.sub_index, bb.sub_index) == (0, 1)


def test_token_to_token_swap_without_reference_price_is_unresolved():
    b = bundle(
        [tf(1, TOKEN, WALLET, POOL, 100 * E18), tf(2, TOKEN2, POOL, WALLET, 50 * E18)],
        tx_meta=meta(WALLET, POOL),
    )
    flows = run(b)
    assert {f.kind for f in flows} == {KIND_SWAP_LEG}
    assert {f.basis_state for f in flows} == {BASIS_UNRESOLVED}
    assert all(f.quote_delta is None for f in flows)


def test_4337_bundle_origin_is_userop_sender():
    tokens, wmon = 200 * E18, 1 * E18
    b = bundle(
        [tf(1, WMON, ACCOUNT, POOL, wmon), tf(2, TOKEN, POOL, ACCOUNT, tokens)],
        tx_meta=meta(BUNDLER, "0x" + "e0" * 20),
        userop_sender=ACCOUNT,
    )
    flows = run(b)
    f = only(flows, ACCOUNT)
    assert len(flows) == 1
    assert f.kind == KIND_BUY
    assert f.origin == ACCOUNT
    assert f.quote_asset == WMON
    assert f.quote_delta == -wmon
    assert f.basis_state == BASIS_OBSERVED


def test_7702_wallet_buys_through_its_own_code():
    tokens, native = 800 * E18, 4 * E18
    b = bundle(
        [tf(1, TOKEN, CORE, SEVEN, tokens)],
        [lt(2, SEVEN, True, native, tokens)],
        meta(SEVEN, SEVEN, 0),
    )
    flows = run(b)
    f = only(flows, SEVEN)
    assert len(flows) == 1
    assert f.kind == KIND_BUY
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.origin == SEVEN
    assert f.venue == CORE


def test_fee_on_transfer_mismatch_still_books_the_single_wallet():
    event_tokens, received, native = 1_000 * E18, 990 * E18, 10 * E18
    b = bundle(
        [tf(1, TOKEN, CORE, ROUTER, event_tokens), tf(2, TOKEN, ROUTER, WALLET, received)],
        [lt(3, ROUTER, True, native, event_tokens)],
        meta(BUNDLER, ROUTER, 0),
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.token_delta == received
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.price_native == Decimal(native) / Decimal(received)


def test_fee_on_transfer_prefers_exact_match_over_near_match():
    native_a, native_b = 10 * E18, 11 * E18
    b = bundle(
        [
            tf(1, TOKEN, CORE, ROUTER, 2_000 * E18),
            tf(2, TOKEN, ROUTER, WALLET, 1_000 * E18),
            tf(3, TOKEN, ROUTER, WALLET2, 1_000 * E18 + 1),
        ],
        [lt(4, ROUTER, True, native_a, 1_000 * E18), lt(5, ROUTER, True, native_b, 1_000 * E18 + 1)],
        meta(BUNDLER, ROUTER, 0),
    )
    flows = run(b)
    assert only(flows, WALLET).quote_delta == -native_a
    assert only(flows, WALLET2).quote_delta == -native_b
    assert all(f.basis_state == BASIS_OBSERVED for f in flows)


def test_otc_native_leg_resolved_by_trace():
    tokens, native = 50 * E18, 9 * E18
    escrow = "0x" + "ee" * 20
    b = bundle(
        [tf(1, TOKEN, WALLET, WALLET2, tokens)],
        tx_meta=meta(WALLET2, escrow, native),
        trace=TraceResult(available=True, transfers=[(WALLET2, escrow, native), (escrow, WALLET, native)]),
    )
    flows = run(b)
    seller = only(flows, WALLET)
    buyer = only(flows, WALLET2)
    assert seller.kind == KIND_SELL
    assert seller.quote_delta == native
    assert seller.source == SOURCE_TRACE
    assert seller.basis_state == BASIS_OBSERVED
    assert buyer.kind == KIND_BUY
    assert buyer.quote_delta == -native
    assert buyer.source == SOURCE_TRANSFER_NET
    assert buyer.basis_state == BASIS_OBSERVED


def test_trace_unavailable_leaves_transfer_unresolved():
    b = bundle(
        [tf(1, TOKEN, WALLET, WALLET2, 50 * E18)],
        tx_meta=meta(WALLET2, "0x" + "ee" * 20, 0),
        trace=TraceResult(available=False),
    )
    flows = run(b)
    assert only(flows, WALLET2).kind == KIND_TRANSFER_IN
    assert only(flows, WALLET2).basis_state == BASIS_UNRESOLVED


def test_wmon_wrap_is_identity_for_quote():
    tokens, native = 100 * E18, 2 * E18
    b = bundle(
        [
            tf(1, WMON, ZERO, WALLET, native),
            tf(2, WMON, WALLET, POOL, native),
            tf(3, TOKEN, POOL, WALLET, tokens),
        ],
        tx_meta=meta(WALLET, WMON, native),
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.quote_asset == NATIVE
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED


def test_lvmon_quote_converted_to_native_units():
    lv_token = "0x" + "1f" * 20
    registry = dict(REGISTRY)
    registry[lv_token] = TokenReg(token=lv_token, source="nadfun_v2", quote_token=LVMON)
    tokens, lvmon = 100 * E18, 4 * E18
    rates = Rates(mon_usd=Decimal(1), lvmon_rate=Decimal("1.25"))
    b = bundle([tf(1, LVMON, WALLET, CORE, lvmon), tf(2, lv_token, CORE, WALLET, tokens)], tx_meta=meta(WALLET, CORE))
    flows = net_transaction(b, registry, kind_of, rates=rates)
    f = only(flows, WALLET, lv_token)
    assert f.quote_asset == LVMON
    assert f.quote_delta == -5 * E18
    assert f.mon_value == Decimal(5)
    b = bundle(
        [tf(2, lv_token, CORE, ROUTER, tokens), tf(3, lv_token, ROUTER, WALLET, tokens)],
        [lt(4, ROUTER, True, lvmon, tokens, token=lv_token)],
        meta(WALLET, ROUTER),
    )
    f = only(net_transaction(b, registry, kind_of, rates=rates), WALLET, lv_token)
    assert f.quote_asset == LVMON
    assert f.quote_delta == -5 * E18
    assert f.source == SOURCE_VENUE_EVENT


def test_usdc_quote_keeps_asset_and_sets_usd_directly():
    tokens, usdc = 100 * E18, 250 * 10**6
    rates = Rates(mon_usd=Decimal(2), usdc_per_mon=Decimal(2))
    b = bundle([tf(1, USDC, WALLET, POOL, usdc), tf(2, TOKEN, POOL, WALLET, tokens)], tx_meta=meta(WALLET, POOL))
    f = only(run(b, rates=rates))
    assert f.kind == KIND_BUY
    assert f.quote_asset == USDC
    assert f.quote_delta == -usdc
    assert f.usd_value == Decimal(250)
    assert f.mon_value == Decimal(125)
    assert f.price_native == Decimal(125) * E18 / Decimal(tokens)


def test_lp_add_parks_tokens():
    tokens, wmon, shares = 100 * E18, 1 * E18, 7 * E18
    b = bundle(
        [tf(1, TOKEN, WALLET, POOL, tokens), tf(2, WMON, WALLET, POOL, wmon), tf(3, POOL, ZERO, WALLET, shares)],
        tx_meta=meta(WALLET, ROUTER),
    )
    f = only(run(b))
    assert f.kind == KIND_LP_ADD
    assert f.basis_state == BASIS_OBSERVED
    assert f.venue == POOL
    assert f.quote_delta is None


def test_venue_from_pool_without_event_is_estimated_at_reference_price():
    tokens = 100 * E18
    b = bundle([tf(1, TOKEN, POOL, WALLET, tokens)], tx_meta=meta(WALLET, ROUTER))
    f = only(run(b, reference_price=lambda t: Decimal("0.02")))
    assert f.kind == KIND_BUY
    assert f.basis_state == BASIS_ESTIMATED
    assert f.source == SOURCE_RECONCILE
    assert f.quote_delta == -2 * E18
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.basis_state == BASIS_UNRESOLVED
    assert f.quote_delta is None


def test_sub_index_is_sorted_by_wallet_then_token_and_deterministic():
    b = bundle(
        [
            tf(5, TOKEN2, WALLET2, WALLET, 1),
            tf(1, TOKEN, WALLET, WALLET2, 1),
            tf(3, TOKEN, WALLET3, WALLET, 1),
        ],
        tx_meta=meta(WALLET3, TOKEN),
    )
    flows = run(b)
    keys = [(f.wallet, f.token, f.sub_index, f.log_index) for f in flows]
    assert keys == sorted(keys)
    assert [f.sub_index for f in flows] == list(range(len(flows)))
    assert len({(f.block_number, f.tx_index, f.log_index, f.sub_index) for f in flows}) == len(flows)
    assert flows == run(b)


def test_routed_buy_split_across_venues_nets_to_one_row():
    a, b_, c = 300 * E18, 500 * E18, 200 * E18
    qa, qb, qc = 3 * E18, 5 * E18, 2 * E18
    pool2 = "0x" + "b2" * 20
    KINDS[pool2] = "venue_pool"
    events = [
        VenueEvent("V3SWAP", 10, {"pool": POOL, "sender": ROUTER, "user": ROUTER, "amount0": -a, "amount1": qa}, POOL),
        VenueEvent("V4SWAP", 11, {"pool_id": "0x1", "sender": ROUTER, "amount0": b_, "amount1": -qb}, POOL_MANAGER),
        VenueEvent(
            "V3SWAP", 12, {"pool": pool2, "sender": ROUTER, "user": ROUTER, "amount0": -c, "amount1": qc}, pool2
        ),
    ]
    transfers = [
        tf(1, TOKEN, POOL, ROUTER, a),
        tf(2, TOKEN, POOL_MANAGER, ROUTER, b_),
        tf(3, TOKEN, pool2, ROUTER, c),
        tf(4, TOKEN, ROUTER, WALLET, a + b_ + c),
        tf(5, WMON, ROUTER, POOL, qa),
        tf(6, WMON, ROUTER, pool2, qc),
    ]
    flows = run(bundle(transfers, events, meta(WALLET, ROUTER, qa + qb + qc)))
    assert len(flows) == 1
    f = flows[0]
    assert f.kind == KIND_BUY
    assert f.token_delta == a + b_ + c
    assert f.quote_delta == -(qa + qb + qc)
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_TRANSFER_NET
    assert f.venue == POOL_MANAGER
    flows = run(bundle(transfers, events, meta(WALLET, ROUTER, 0)))
    f = flows[0]
    assert f.quote_delta == -(qa + qb + qc)
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT


def test_unregistered_token_and_passthrough_produce_nothing():
    other = "0x" + "99" * 20
    b = bundle([tf(1, other, WALLET, WALLET2, 5), tf(2, TOKEN, SETTLER, ROUTER, 5)], tx_meta=meta(WALLET, ROUTER))
    assert run(b) == []
