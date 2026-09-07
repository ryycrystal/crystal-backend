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


MARKET = "0x664fdc46471fd3b407a94e61bc18129abbee3171"
MARKETS = {MARKET: (TOKEN, WMON)}


def tr(idx: int, user: str, is_buy: bool, native: int, tokens: int, address: str = CORE) -> VenueEvent:
    parsed = {
        "market": MARKET,
        "user": user,
        "is_buy": is_buy,
        "amount_in": native if is_buy else tokens,
        "amount_out": tokens if is_buy else native,
        "start_price": 0,
        "end_price": 0,
    }
    return VenueEvent(tag="TR", log_index=idx, parsed=parsed, address=address)


def test_graduated_fill_sell_via_settler_resolves_from_the_market_map():
    tokens, native = 162_232_261 * E18, 6_968 * E18
    b = bundle(
        [tf(3, TOKEN, WALLET, SETTLER, tokens), tf(4, TOKEN, SETTLER, CORE, tokens)],
        [tr(5, SETTLER, False, native, tokens)],
        meta(WALLET, SETTLER, 0),
    )
    flows = run(b, markets=MARKETS)
    assert [f.wallet for f in flows] == [WALLET]
    f = flows[0]
    assert f.kind == KIND_SELL
    assert f.token_delta == -tokens
    assert f.quote_asset == NATIVE
    assert f.quote_delta == native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.venue in (CORE, MARKET)


def test_graduated_fill_without_a_market_map_stays_unpriced_not_invented():
    tokens, native = 162_232_261 * E18, 6_968 * E18
    b = bundle(
        [tf(3, TOKEN, WALLET, SETTLER, tokens), tf(4, TOKEN, SETTLER, CORE, tokens)],
        [tr(5, SETTLER, False, native, tokens)],
        meta(WALLET, SETTLER, 0),
    )
    f = only(run(b))
    assert f.basis_state != BASIS_OBSERVED


def test_graduated_fill_buy_through_router_marks_venue_and_price_from_the_fill():
    tokens, native = 933_619_875 * E18, 8_598 * E18
    b = bundle(
        [tf(2, TOKEN, CORE, ROUTER, tokens), tf(3, TOKEN, ROUTER, WALLET, tokens)],
        [tr(4, ROUTER, True, native, tokens)],
        meta(WALLET, ROUTER, native),
    )
    f = only(run(b, markets=MARKETS))
    assert f.kind == KIND_BUY
    assert f.token_delta == tokens
    assert f.quote_asset == NATIVE
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED
    assert f.venue in (CORE, MARKET)


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
    assert [f.sub_index for f in flows] == [0, 0, 0]


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
    assert (a.sub_index, bb.sub_index) == (0, 0)


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


def test_flow_identity_comes_from_the_movement_not_its_ordinal():
    b = bundle(
        [
            tf(5, TOKEN2, WALLET2, WALLET, 1),
            tf(1, TOKEN, WALLET, WALLET2, 1),
            tf(3, TOKEN, WALLET3, WALLET, 1),
        ],
        tx_meta=meta(WALLET3, TOKEN),
    )
    flows = run(b)
    assert len({(f.block_number, f.tx_index, f.log_index, f.sub_index) for f in flows}) == len(flows), (
        "both halves of a transfer share a log index, so the stored key must still separate them"
    )
    for f in flows:
        assert f.sub_index == (0 if f.token_delta < 0 else 1), "the outgoing half folds first"
    assert flows == run(b)


def test_both_halves_of_a_wallet_to_wallet_transfer_survive_being_stored():
    """One log, two movements. Keyed on the log alone they collide and the second is dropped on insert."""
    b = bundle([tf(3, TOKEN, WALLET, WALLET2, 100 * E18)], tx_meta=meta(WALLET, WALLET2))
    flows = run(b)
    assert {f.wallet for f in flows} == {WALLET, WALLET2}
    keys = {(f.block_number, f.tx_index, f.log_index, f.sub_index) for f in flows}
    assert len(keys) == 2, "the sender and the receiver must not share a primary key"
    out = next(f for f in flows if f.token_delta < 0)
    into = next(f for f in flows if f.token_delta > 0)
    assert out.sub_index < into.sub_index, "the sender must fold before the receiver at one chain position"


def test_a_self_transfer_keeps_two_movements_apart():
    b = bundle([tf(4, TOKEN, WALLET, WALLET, 5 * E18)], tx_meta=meta(WALLET, WALLET))
    flows = run(b)
    assert len({(f.log_index, f.sub_index) for f in flows}) == len(flows)


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


def test_routed_buy_books_what_the_wallet_paid_including_the_router_fee():
    tokens = 933_619_875_239_020_431_266_724_571
    venue_native = 8_598_080_700_000_000_194_641
    paid = 8_684_930_000_000_000_196_608
    b = bundle(
        [tf(55, TOKEN, CORE, ROUTER, tokens), tf(56, TOKEN, ROUTER, WALLET, tokens)],
        [lt(54, ROUTER, True, venue_native, tokens)],
        meta(WALLET, ROUTER, paid),
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.quote_asset == NATIVE
    assert f.quote_delta == -paid
    assert f.basis_state == BASIS_OBSERVED
    assert f.venue == CORE


def test_routed_sell_books_the_receipt_the_wallet_actually_kept():
    tokens, venue_native = 162_232_261 * E18, 6_968 * E18
    received = venue_native - venue_native // 100
    b = bundle(
        [tf(26, TOKEN, WALLET, SETTLER, tokens), tf(29, TOKEN, SETTLER, CORE, tokens)],
        [lt(28, SETTLER, False, venue_native, tokens)],
        meta(WALLET, SETTLER, 0),
        trace=TraceResult(available=True, transfers=[(CORE, SETTLER, venue_native), (SETTLER, WALLET, received)]),
    )
    f = only(run(b))
    assert f.kind == KIND_SELL
    assert f.quote_delta == received
    assert f.basis_state == BASIS_OBSERVED


def test_split_curve_fills_book_what_the_wallet_sent():
    t1, t2 = 1_346_767 * E18, 5_123_708 * E18
    n1, n2 = 20 * E18, 79 * E18
    b = bundle(
        [tf(126, TOKEN, CORE, ROUTER, t1), tf(134, TOKEN, CORE, ROUTER, t2), tf(135, TOKEN, ROUTER, WALLET, t1 + t2)],
        [lt(127, ROUTER, True, n1, t1), lt(133, ROUTER, True, n2, t2)],
        meta(WALLET, ROUTER, 100 * E18),
    )
    f = only(run(b))
    assert f.quote_delta == -100 * E18
    assert f.basis_state == BASIS_OBSERVED


def test_own_quote_far_from_the_venue_amount_is_kept():
    tokens, venue_native = 500 * E18, 3 * E18
    b = bundle(
        [tf(2, TOKEN, CORE, ROUTER, tokens), tf(3, TOKEN, ROUTER, WALLET, tokens)],
        [lt(4, ROUTER, True, venue_native, tokens)],
        meta(WALLET, ROUTER, 6 * E18),
    )
    f = only(run(b))
    assert f.quote_delta == -6 * E18
    assert f.source == SOURCE_TRANSFER_NET
    assert f.venue == CORE


def test_pool_event_orientation_needs_a_matching_transfer_at_the_venue():
    tokens, wmon = 400 * E18, 4 * E18
    hedge_native, hedge_usdc = 5 * E18, 1
    swap = VenueEvent(
        "V3SWAP", 12, {"pool": POOL, "sender": ROUTER, "user": ROUTER, "amount0": -tokens, "amount1": wmon}, POOL
    )
    hedge = VenueEvent(
        "V4SWAP",
        14,
        {"pool_id": "0x02", "sender": ROUTER, "amount0": -hedge_native, "amount1": hedge_usdc},
        POOL_MANAGER,
    )
    b = bundle(
        [tf(10, WMON, ROUTER, POOL, wmon), tf(11, TOKEN, POOL, ROUTER, tokens), tf(13, TOKEN, ROUTER, WALLET, tokens)],
        [swap, hedge],
        meta(WALLET, ROUTER),
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.quote_asset == WMON
    assert f.quote_delta == -wmon
    assert f.basis_state == BASIS_OBSERVED
    assert f.venue == POOL


def test_partially_covered_single_leg_is_scaled_at_the_venue_price():
    from_pool, otc, wmon = 600 * E18, 200 * E18, 6 * E18
    swap = VenueEvent(
        "V3SWAP", 5, {"pool": POOL, "sender": ROUTER, "user": ROUTER, "amount0": -from_pool, "amount1": wmon}, POOL
    )
    b = bundle(
        [
            tf(1, WMON, ROUTER, POOL, wmon),
            tf(2, TOKEN, POOL, ROUTER, from_pool),
            tf(3, TOKEN, WALLET2, ROUTER, otc),
            tf(4, TOKEN, ROUTER, WALLET, from_pool + otc),
        ],
        [swap],
        meta(BUNDLER, ROUTER),
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.token_delta == from_pool + otc
    assert f.quote_asset == WMON
    assert f.quote_delta == -8 * E18
    assert f.basis_state == BASIS_ESTIMATED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.price_native == Decimal(wmon) / Decimal(from_pool)
    assert f.venue == POOL


def test_routed_buy_sourced_from_two_pools_and_an_otc_seller_sums_to_an_observed_cost():
    seller = WALLET2
    executor = BATCHER
    pool_tokens, pool_wmon = 3_939_541 * E18, 88_936 * E18
    v4_tokens, v4_wmon = 2_917_301 * E18, 68_724 * E18
    otc_tokens, otc_wmon = 182_149 * E18, 4_042 * E18
    total = pool_tokens + v4_tokens + otc_tokens
    v3 = VenueEvent(
        tag="V3SWAP",
        log_index=10,
        parsed={"pool": POOL, "sender": executor, "user": executor, "amount0": -pool_tokens, "amount1": pool_wmon},
        address=POOL,
    )
    v4 = VenueEvent(
        tag="V4SWAP",
        log_index=11,
        parsed={"pool_id": "0x01", "sender": executor, "amount0": v4_tokens, "amount1": -v4_wmon},
        address=POOL_MANAGER,
    )
    b = bundle(
        [
            tf(1, WMON, executor, POOL, pool_wmon),
            tf(2, TOKEN, POOL, executor, pool_tokens),
            tf(3, TOKEN, POOL_MANAGER, executor, v4_tokens),
            tf(4, TOKEN, seller, executor, otc_tokens),
            tf(5, WMON, executor, seller, otc_wmon),
            tf(6, TOKEN, executor, ROUTER, total),
            tf(7, TOKEN, ROUTER, WALLET, total),
        ],
        [v3, v4],
        meta(BUNDLER, executor, 0),
    )
    flows = run(b)
    buyer = only(flows)
    assert buyer.kind == KIND_BUY
    assert buyer.token_delta == total
    assert buyer.quote_delta == -(pool_wmon + v4_wmon + otc_wmon)
    assert buyer.basis_state == BASIS_OBSERVED
    seller_flow = only(flows, wallet=seller)
    assert seller_flow.kind == KIND_SELL
    assert seller_flow.quote_delta == otc_wmon
    assert seller_flow.basis_state == BASIS_OBSERVED


def test_v4_singleton_quote_asset_is_matched_per_pool_not_per_manager():
    tokens, native = 300 * E18, 7 * E18
    hedge_native, hedge_usdc = 5 * E18, 120_000_000
    token_swap = VenueEvent(
        "V4SWAP", 5, {"pool_id": "0x01", "sender": ROUTER, "amount0": tokens, "amount1": -native}, POOL_MANAGER
    )
    hedge = VenueEvent(
        "V4SWAP",
        7,
        {"pool_id": "0x02", "sender": ROUTER, "amount0": -hedge_native, "amount1": hedge_usdc},
        POOL_MANAGER,
    )
    b = bundle(
        [
            tf(4, TOKEN, POOL_MANAGER, ROUTER, tokens),
            tf(6, TOKEN, ROUTER, WALLET, tokens),
            tf(8, USDC, POOL_MANAGER, ROUTER, hedge_usdc),
        ],
        [token_swap, hedge],
        meta(BUNDLER, ROUTER),
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.quote_asset == NATIVE
    assert f.quote_delta == -native
    assert f.basis_state == BASIS_OBSERVED
    assert f.source == SOURCE_VENUE_EVENT
    assert f.venue == POOL_MANAGER


def test_v4_wmon_pool_quote_asset_follows_the_matching_transfer():
    tokens, wmon = 300 * E18, 7 * E18
    swap = VenueEvent(
        "V4SWAP", 5, {"pool_id": "0x01", "sender": ROUTER, "amount0": tokens, "amount1": -wmon}, POOL_MANAGER
    )
    b = bundle(
        [
            tf(3, WMON, ROUTER, POOL_MANAGER, wmon),
            tf(4, TOKEN, POOL_MANAGER, ROUTER, tokens),
            tf(6, TOKEN, ROUTER, WALLET, tokens),
            tf(8, USDC, POOL_MANAGER, ROUTER, 5_000_000),
        ],
        [swap],
        meta(BUNDLER, ROUTER),
    )
    f = only(run(b))
    assert f.quote_asset == WMON
    assert f.quote_delta == -wmon
    assert f.basis_state == BASIS_OBSERVED


def test_an_estimate_that_rounds_to_nothing_is_unresolved_not_a_free_trade():
    b = bundle([tf(1, TOKEN, POOL, WALLET, 5)], [], meta(WALLET, ROUTER, 0))
    f = only(run(b, reference_price=lambda token: Decimal("0.000000001")))
    assert f.basis_state == BASIS_UNRESOLVED
    assert f.quote_delta is None


def test_a_small_transfer_beside_a_large_one_names_its_own_recipient():
    """A wallet paying a fee alongside a bigger transfer has two counterparties, not one.

    Naming the largest neighbour of the transaction was right while a leg was the wallet's whole netted
    position. It is wrong for a single movement, and it is what put the wrong address on 2,751 of JAMES's
    transfer rows and would send parked basis to the wrong vault.
    """
    fee_taker = "0x" + "fe" * 20
    KINDS[fee_taker] = "eoa"
    b = bundle(
        [
            tf(33, TOKEN, WALLET, WALLET2, 698 * E18),
            tf(34, TOKEN, WALLET, fee_taker, 2 * E18),
        ],
        tx_meta=meta(WALLET, WALLET2),
    )
    flows = run(b)
    outgoing = {f.log_index: f for f in flows if f.token_delta < 0}
    assert outgoing[33].counterparty == WALLET2
    assert outgoing[34].counterparty == fee_taker, "the fee leg must name who received the fee"
    incoming = {f.log_index: f for f in flows if f.token_delta > 0}
    assert incoming[33].wallet == WALLET2 and incoming[33].counterparty == WALLET
    assert incoming[34].wallet == fee_taker and incoming[34].counterparty == WALLET


def test_a_wallet_sending_tokens_to_itself_is_not_a_swap():
    """Two opposite movements of ONE token are a self-transfer; a swap is two different tokens.

    Priced as a swap the pair invents a sale and a purchase out of a movement that changed nothing, which
    net quantity cannot see. Three of JAMES's transactions have this shape.
    """
    b = bundle([tf(188, TOKEN, WALLET, WALLET, 265 * E18)], tx_meta=meta(WALLET, TOKEN))
    flows = [f for f in run(b, reference_price=lambda _t: Decimal("0.5")) if f.wallet == WALLET]
    assert {f.kind for f in flows} == {KIND_TRANSFER_OUT, KIND_TRANSFER_IN}, [f.kind for f in flows]
    assert all(f.quote_delta is None for f in flows), "nothing was bought or sold"
    assert sum(f.token_delta for f in flows) == 0


def test_a_token_to_token_swap_is_still_a_swap():
    b = bundle(
        [tf(4, TOKEN, WALLET, POOL, 100 * E18), tf(5, TOKEN2, POOL, WALLET, 50 * E18)],
        tx_meta=meta(WALLET, POOL),
    )
    flows = [f for f in run(b, reference_price=lambda _t: Decimal("0.5")) if f.wallet == WALLET]
    assert {f.kind for f in flows} == {KIND_SWAP_LEG}, [f.kind for f in flows]


def v3swap(idx, pool, sender, amount0, amount1):
    return VenueEvent(
        tag="V3SWAP",
        log_index=idx,
        parsed={"pool": pool, "sender": sender, "user": sender, "amount0": amount0, "amount1": amount1},
        address=pool,
    )


def test_a_pair_swap_that_moved_no_token_here_moved_it_somewhere_else():
    """Transfer into the pair, then call swap: the disposal belongs to the transfer, not to the later call.

    Uniswap V4 can settle a swap against internal claim balances, so a V4 event with no transfer really is
    the movement. A V2 or V3 pair cannot: it always moves the ERC-20 in the same transaction, so an absent
    transfer means the tokens arrived in an earlier one and are already recorded. Reading the event as a
    movement booked one of JAMES's wallets as selling the same tokens twice, into a negative balance.
    """
    pools = {POOL: (TOKEN, WMON, True)}
    b = bundle(
        [tf(91, WMON, POOL, WALLET, 200 * E18)], [v3swap(90, POOL, WALLET, 777 * E18, -200 * E18)], meta(WALLET, POOL)
    )
    assert [f for f in run(b, pools=pools) if f.token == TOKEN] == [], "the tokens moved in another transaction"


def test_a_pair_swap_naming_a_quote_that_never_moved_is_not_a_price():
    """A pair reports both sides as ERC-20 movements, so a quote no transfer corroborates is not evidence.

    Two of JAMES's flows took an uncorroborated pair amount as the price paid and booked 554 trillion MON of
    cost between them, against a token whose median price is 0.06 MON. The amount is accepted when a real
    movement of that size backs it, and ignored when nothing does.
    """
    absurd = 523_137_636_709_952_419_125_781_229_257_940
    b = bundle(
        [tf(216, TOKEN, POOL, WALLET, 878 * E18), tf(217, WMON, POOL, WALLET, 3 * E18)],
        [v3swap(218, POOL, WALLET, -878 * E18, absurd)],
        meta(WALLET, POOL),
    )
    f = only(run(b))
    assert f.quote_delta is None or abs(f.quote_delta) <= 3 * E18, (
        f"the wallet received 3 WMON; {f.quote_delta} came from the event alone"
    )


def test_a_pair_swap_whose_quote_did_move_is_still_a_price():
    b = bundle(
        [tf(4, WMON, WALLET, POOL, 3 * E18), tf(5, TOKEN, POOL, WALLET, 878 * E18)],
        [v3swap(6, POOL, WALLET, -878 * E18, 3 * E18)],
        meta(WALLET, POOL),
    )
    f = only(run(b))
    assert f.kind == KIND_BUY
    assert f.quote_delta == -3 * E18
    assert f.basis_state == BASIS_OBSERVED


def test_no_flow_is_priced_above_every_quote_that_moved_in_its_transaction():
    """A relayer forwards what it received and is paid in another asset, keeping wei of dust.

    A wallet's implied price has to come from what it sent, not from what it was left holding. Measured
    from the net position this shape gives a price twelve orders of magnitude too high, which then prices
    the movement that really passed through. This asserts the ceiling rather than that shape: the real
    reproduction is JAMES transaction 0x9b3623e855d9 at block 89,459,208, where it booked 523 trillion MON
    of cost against a token trading at 0.06, and which now nets to 0.0595 MON per token.
    """
    dust = 99_921_408
    b = bundle(
        [
            tf(211, WMON, POOL, WALLET2, 59 * E18),
            tf(217, TOKEN, WALLET3, WALLET2, 878 * E18),
            tf(218, TOKEN, WALLET2, WALLET, 878 * E18 + dust),
        ],
        tx_meta=meta(WALLET3, WALLET2),
    )
    flows = run(b, rates=Rates(mon_usd=Decimal(1), usdc_per_mon=Decimal(1)))
    for f in flows:
        assert abs(f.quote_delta or 0) <= 59 * E18, (
            f"{f.wallet[:10]} {f.kind}: {f.quote_delta} exceeds every quote that moved in this transaction"
        )


def test_a_wallets_quote_is_conserved_across_its_movements_even_when_split_oddly():
    """Which of a wallet's own sales a payment is attached to can be wrong; the total cannot.

    A wallet selling one holding across three movements in one transaction has its receipts assigned to the
    nearest movement of opposite sign, and proximity is not always right: on JAMES transaction 0xf8f9f66354
    a 0.48-token movement was given 5,729 MON while a 33,605-token movement was given a fraction of a wei.
    Under average cost the position is unaffected, because realized comes from the totals; only the
    per-movement `price_native` is distorted, and that is what a later reference price can read. Measured on
    JAMES, the flows more than a thousandfold from the median carry 0.010% of traded value.
    """
    b = bundle(
        [
            tf(121, WMON, WALLET, POOL, 100 * E18),
            tf(122, TOKEN, POOL, WALLET, 1000 * E18),
            tf(130, TOKEN, WALLET, POOL, 999 * E18),
            tf(131, WMON, POOL, WALLET, 60 * E18),
            tf(138, TOKEN, WALLET, POOL, 1 * E18),
            tf(139, WMON, POOL, WALLET, 90 * E18),
        ],
        tx_meta=meta(WALLET, POOL),
    )
    flows = [f for f in run(b) if f.wallet == WALLET and f.token == TOKEN]
    assert sum(f.quote_delta or 0 for f in flows) == -100 * E18 + 60 * E18 + 90 * E18
    assert sum(f.token_delta for f in flows) == 0


def test_a_venue_event_still_prices_a_partial_fill():
    b = bundle(
        [tf(4, WMON, WALLET, POOL, 10 * E18), tf(5, TOKEN, POOL, WALLET, 50 * E18)],
        [v3swap(6, POOL, WALLET, -100 * E18, 20 * E18)],
        meta(WALLET, POOL),
    )
    f = only(run(b))
    assert f.quote_delta is not None and abs(f.quote_delta) <= 20 * E18


def test_two_halves_that_agree_on_the_price_are_a_trade():
    b = bundle(
        [
            tf(103, TOKEN, WALLET, WALLET2, 100 * E18),
            tf(104, USDC, WALLET2, WALLET, 500_000_000),
        ],
        tx_meta=meta(WALLET2, WALLET),
    )
    halves = {f.wallet: f for f in run(b) if f.token == TOKEN}
    assert halves[WALLET].kind == KIND_SELL and halves[WALLET2].kind == KIND_BUY
    assert halves[WALLET].quote_delta == 500_000_000
    assert halves[WALLET2].quote_delta == -500_000_000
