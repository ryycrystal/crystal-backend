"""Acceptance fixtures for the seam repair (REPAIR_PLAN.md v2), written to fail first.

Every defect these target preserves net token quantity per wallet, which is exactly why the existing
balance comparisons, supply conservation and SQL invariants all pass while cost, proceeds, confidence and
identity are wrong. None of these can be expressed as a quantity check.

The rule these exist to satisfy, from feedback8.md: a check must be shown to FAIL on the defect it targets
before it is allowed to pass on the fix. Each was written as a failing test first, marked xfail(strict=True) against the
current engine. As a seam is repaired its fixtures start passing and pytest reports XPASS as a failure,
which is the signal to drop the marker. A test that stops failing for the wrong reason is caught the same way.

Numbers come from feedback7.md's acceptance table, which measured the "today" column against this code.
"""

from decimal import Decimal

from core.ledger.fold import PositionState, fold  # noqa: E402
from core.ledger.netflow import net_transaction  # noqa: E402
from core.ledger.types import (  # noqa: E402
    BASIS_ESTIMATED,
    BASIS_OBSERVED,
    BASIS_UNRESOLVED,
    KIND_BUY,
    KIND_SELL,
    KIND_TRANSFER_IN,
    NATIVE,
    USDC,
    WMON,
    Rates,
    VenueEvent,
)
from tests.test_ledger_fold import _flow as fold_flow
from tests.test_ledger_netflow import (
    CORE,
    E18,
    POOL,
    REGISTRY,
    TOKEN,
    WALLET,
    WALLET2,
    WALLET3,
    bundle,
    kind_of,
    lt,
    meta,
    only,
    run,
    tf,
)

RATES = Rates(mon_usd=Decimal(1), usdc_per_mon=Decimal(1))
FORGER = "0x" + "f0" * 20
VAULT_A = "0x" + "a5" * 20
VAULT_B = "0x" + "b6" * 20


def test_02_an_unequal_round_trip_keeps_both_actions():
    """Buy 100 at 100 and sell 60 at 72 in one transaction. Today: one flow, +40 for -28 WMON."""
    b = bundle(
        [
            tf(4, WMON, WALLET, POOL, 100 * E18),
            tf(5, TOKEN, POOL, WALLET, 100 * E18),
            tf(8, TOKEN, WALLET, POOL, 60 * E18),
            tf(9, WMON, POOL, WALLET, 72 * E18),
        ],
        tx_meta=meta(WALLET, POOL),
    )
    flows = [f for f in run(b, rates=RATES) if f.wallet == WALLET and f.token == TOKEN]
    buys = [f for f in flows if f.kind == KIND_BUY]
    sells = [f for f in flows if f.kind == KIND_SELL]
    assert len(buys) == 1 and len(sells) == 1, flows
    assert buys[0].token_delta == 100 * E18
    assert buys[0].quote_delta == -100 * E18
    assert sells[0].token_delta == -60 * E18
    assert sells[0].quote_delta == 72 * E18


def test_03_sell_then_buy_keeps_its_own_identity():
    """Sell at log 2, buy at log 7. Today: one flow anchored at 2 but labelled a buy."""
    b = bundle(
        [
            tf(2, TOKEN, WALLET, POOL, 60 * E18),
            tf(3, WMON, POOL, WALLET, 72 * E18),
            tf(7, WMON, WALLET, POOL, 100 * E18),
            tf(8, TOKEN, POOL, WALLET, 100 * E18),
        ],
        tx_meta=meta(WALLET, POOL),
    )
    flows = [f for f in run(b, rates=RATES) if f.wallet == WALLET and f.token == TOKEN]
    assert len(flows) == 2, flows
    earliest = min(flows, key=lambda f: f.log_index)
    assert earliest.kind == KIND_SELL


def test_04_a_gift_and_an_unrelated_payment_are_not_a_purchase():
    """A is gifted 100 by B and separately pays 10 WMON to C. Today: an observed buy at -10 WMON."""
    b = bundle(
        [
            tf(3, TOKEN, WALLET2, WALLET, 100 * E18),
            tf(4, WMON, WALLET, WALLET3, 10 * E18),
        ],
        tx_meta=meta(WALLET, WALLET3),
    )
    f = only(run(b, rates=RATES))
    assert f.kind == KIND_TRANSFER_IN
    assert f.basis_state == BASIS_UNRESOLVED
    assert not f.quote_delta


def test_08_a_forged_venue_event_cannot_manufacture_a_purchase():
    """A wallet-to-wallet transfer plus a launchpad trade event from an unaccepted address."""
    forged = lt(5, WALLET, True, 10 * E18, 100 * E18, address=FORGER)
    b = bundle(
        [tf(3, TOKEN, WALLET2, WALLET, 100 * E18)],
        [forged],
        meta(WALLET, WALLET2),
    )
    f = only(run(b, rates=RATES))
    assert f.kind == KIND_TRANSFER_IN
    assert f.basis_state == BASIS_UNRESOLVED


def test_09_a_purchase_funded_in_two_quotes_conserves_both():
    """10 WMON + 20 USDC at 1 USD/MON for 100 tokens. Today: 20 MON recorded, a third understated."""
    b = bundle(
        [
            tf(4, WMON, WALLET, POOL, 10 * E18),
            tf(5, USDC, WALLET, POOL, 20 * 10**6),
            tf(6, TOKEN, POOL, WALLET, 100 * E18),
        ],
        tx_meta=meta(WALLET, POOL),
    )
    f = only(run(b, rates=RATES))
    assert f.kind == KIND_BUY
    assert abs(f.quote_delta) == 30 * E18


def test_10_a_router_fee_is_retained_not_erased():
    """The wallet pays 109.90 MON, the venue receives 100.00. Today the 9.90 is deleted."""
    paid = 1099 * E18 // 10
    b = bundle(
        [
            tf(4, WMON, WALLET, CORE, paid),
            tf(5, WMON, CORE, POOL, 100 * E18),
            tf(6, TOKEN, POOL, WALLET, 100 * E18),
        ],
        [lt(7, WALLET, True, 100 * E18, 100 * E18)],
        meta(WALLET, CORE),
    )
    f = only(run(b, rates=RATES))
    assert abs(f.quote_delta) == paid, "the fee must stay in cost, or be recorded separately"


def test_11_flow_identity_survives_a_registry_change():
    """Registering a token that sorts earlier must not renumber an existing token's key."""
    legs = [
        tf(4, WMON, WALLET, POOL, 10 * E18),
        tf(5, TOKEN, POOL, WALLET, 100 * E18),
    ]
    before = net_transaction(bundle(legs, tx_meta=meta(WALLET, POOL)), REGISTRY, kind_of, rates=RATES)
    earlier = "0x" + "01" * 20
    wider = dict(REGISTRY)
    wider[earlier] = REGISTRY[TOKEN].__class__(token=earlier, source="crystal", registered_block=1)
    legs_with_other = legs + [tf(6, earlier, POOL, WALLET, 5 * E18)]
    after = net_transaction(bundle(legs_with_other, tx_meta=meta(WALLET, POOL)), wider, kind_of, rates=RATES)

    def key(f):
        return (f.txhash, f.wallet, f.token, f.sub_index)

    keyed_before = {key(f): f for f in before if f.token == TOKEN}
    keyed_after = {key(f): f for f in after if f.token == TOKEN}
    assert set(keyed_before) == set(keyed_after), "the same movement changed key when another token registered"


def test_01_a_claim_settled_swap_is_a_movement_without_a_transfer():
    """Uniswap V4 can settle against claim balances, moving no ERC-20. Today: no rows at all."""
    swap = VenueEvent(
        tag="V4SWAP",
        log_index=11,
        parsed={"pool_id": "0x01", "sender": WALLET, "amount0": 320 * E18, "amount1": -1 * 10**6},
        address=POOL,
    )
    b = bundle([], [swap], meta(WALLET, POOL))
    pools = {"0x01": (TOKEN, USDC, True)}
    flows = [f for f in run(b, rates=RATES, pools=pools) if f.token == TOKEN]
    assert flows, "a claim-settled swap must still produce a movement"
    f = flows[0]
    assert f.wallet == WALLET
    assert f.token_delta == 320 * E18
    assert f.kind == KIND_BUY


def test_the_defects_these_fixtures_target_are_invisible_to_a_quantity_check():
    """The control: net quantity per wallet is right in the round-trip case, which is why nothing caught it."""
    b = bundle(
        [
            tf(4, WMON, WALLET, POOL, 100 * E18),
            tf(5, TOKEN, POOL, WALLET, 100 * E18),
            tf(8, TOKEN, WALLET, POOL, 60 * E18),
            tf(9, WMON, POOL, WALLET, 72 * E18),
        ],
        tx_meta=meta(WALLET, POOL),
    )
    flows = [f for f in run(b, rates=RATES) if f.wallet == WALLET and f.token == TOKEN]
    assert sum(f.token_delta for f in flows) == 40 * E18
    assert all(f.basis_state in (BASIS_OBSERVED, BASIS_UNRESOLVED) for f in flows)
    assert NATIVE or True


def test_06_an_unpriced_disposal_holds_its_basis_out_of_the_running_average():
    """Buy 100 at 100 observed, then sell 100 with no observable proceeds. Today: realized_estimated -100."""
    flows = [
        fold_flow(KIND_BUY, 100, -100, block=1),
        fold_flow(KIND_SELL, -100, None, block=2, basis_state=BASIS_UNRESOLVED),
    ]
    state, _ = fold(None, flows)
    assert state.realized_estimated_native == 0, "an unknown price is not a loss"
    assert state.realized_pnl_native == 0
    assert state.cost_basis_native == 0, "the basis must leave the running average"
    assert state.observed_tokens == 0
    assert getattr(state, "disposed_unresolved_basis_native", None) == 100
    assert getattr(state, "disposed_unresolved_tokens", None) == 100


def test_07_a_mixed_inventory_sale_persists_the_vector_the_fold_computed():
    """100 observed at 100, 100 estimated at 300, 100 unresolved; sell 150 at 600. The row must carry the split."""
    flows = [
        fold_flow(KIND_BUY, 100, -100, block=1),
        fold_flow(KIND_BUY, 100, -300, block=2, basis_state=BASIS_ESTIMATED),
        fold_flow(KIND_TRANSFER_IN, 100, None, block=3, basis_state=BASIS_UNRESOLVED),
        fold_flow(KIND_SELL, -150, 600, block=4),
    ]
    state, out = fold(None, flows)
    sale = out[-1]
    assert (state.realized_pnl_native, state.realized_estimated_native, state.unresolved_proceeds_native) == (
        150,
        50,
        200,
    )
    assert getattr(sale, "basis_observed_delta", None) == -50
    assert getattr(sale, "basis_estimated_delta", None) == -150
    assert getattr(sale, "qty_observed", None) == -50
    assert getattr(sale, "qty_estimated", None) == -50
    assert getattr(sale, "qty_unresolved", None) == -50
    assert getattr(sale, "realized_observed_delta", None) == 150
    assert getattr(sale, "realized_estimated_delta", None) == 50
    assert getattr(sale, "unresolved_proceeds_delta", None) == 200
    assert sale.basis_delta == -200 and sale.realized_delta == 200


def test_a_position_resumed_from_its_stored_row_folds_like_one_folded_whole():
    """The persisted row must be a checkpoint: resuming from it gives the same result as folding everything."""
    tx = "0x" + "ab" * 32
    flows = [
        fold_flow(KIND_BUY, 100, -100, block=1, log_index=3, txhash=tx),
        fold_flow(KIND_BUY, 50, -50, block=1, log_index=7, txhash=tx),
        fold_flow(KIND_SELL, -150, 300, block=2),
    ]
    whole, _ = fold(None, flows)
    first, _ = fold(None, flows[:1])
    resumed = PositionState.from_row(first.to_row(), first.parked)
    second, _ = fold(resumed, flows[1:])
    assert second.realized_pnl_native == whole.realized_pnl_native == 150
    assert second.trade_count == whole.trade_count == 2
    assert second.to_row() == whole.to_row()


def test_10b_parked_basis_belongs_to_the_place_it_was_parked_in():
    """Park 100 at 100 in vault A and 100 at 1,000 in vault B, then withdraw from A.

    Today the two are one pooled aggregate, so withdrawing from A restores the average of both: 550 instead
    of the 100 that was actually parked there. Nothing about the withdrawal says which vault it came from
    because the basis was never told where it went.
    """
    flows = [
        fold_flow(KIND_BUY, 100, -100, block=1),
        fold_flow("vault_deposit", -100, None, block=2, counterparty=VAULT_A),
        fold_flow(KIND_BUY, 100, -1000, block=3),
        fold_flow("vault_deposit", -100, None, block=4, counterparty=VAULT_B),
        fold_flow("vault_withdraw", 100, None, block=5, counterparty=VAULT_A),
    ]
    state, out = fold(None, flows)
    assert out[-1].basis_delta == 100, "the withdrawal restores what was parked in A, not the pooled average"
    assert state.cost_basis_native == 100
    assert state.parked_observed_basis == 1000, "B's basis stays parked in B"
    assert state.parked_observed_tokens == 100


def test_10c_withdrawing_from_a_place_nothing_was_parked_in_restores_nothing():
    flows = [
        fold_flow(KIND_BUY, 100, -100, block=1),
        fold_flow("lp_add", -100, None, block=2, counterparty=VAULT_A),
        fold_flow("lp_remove", 100, None, block=3, counterparty=VAULT_B),
    ]
    state, out = fold(None, flows)
    assert out[-1].basis_delta == 0, "B holds nothing of this wallet's, so nothing comes back priced"
    assert state.unresolved_tokens == 100
    assert state.parked_observed_basis == 100, "A still holds the parked basis"


SENDER_LOW = "0x" + "11" * 20
SENDER_HIGH = "0x" + "ee" * 20
RECEIVER_MID = "0x" + "88" * 20


def _transfer_pair(sender, receiver, block, log_index, amount):
    """The two halves of one transfer: one log, two movements, the outgoing one folding first."""
    return [
        fold_flow(
            "transfer_out",
            -amount,
            None,
            block=block,
            log_index=log_index,
            sub_index=0,
            wallet=sender,
            counterparty=receiver,
        ),
        fold_flow(
            "transfer_in",
            amount,
            None,
            block=block,
            log_index=log_index,
            sub_index=1,
            wallet=receiver,
            counterparty=sender,
        ),
    ]


def _cross_wallet_case(sender, receiver):
    return [
        fold_flow(KIND_BUY, 100, -100, block=1, wallet=sender),
        *_transfer_pair(sender, receiver, block=2, log_index=4, amount=100),
        fold_flow(KIND_SELL, -100, 150, block=3, wallet=receiver),
    ]


def test_05_basis_travels_with_a_transfer_whichever_way_the_addresses_sort():
    """The sender's released basis is the receiver's cost. Today the receiver books it as unresolved.

    Both address orders must give the same answer: the two halves of a transfer share a chain position, so
    an order that came from sorting on the address decided which wallet folded first, and in 56.8% of real
    pairs that was the receiver.
    """
    from core.ledger.fold import fold_token

    for sender, receiver in ((SENDER_LOW, RECEIVER_MID), (SENDER_HIGH, RECEIVER_MID)):
        states, out = fold_token(None, _cross_wallet_case(sender, receiver))
        arrival = next(f for f in out if f.kind == KIND_TRANSFER_IN)
        assert arrival.basis_delta == 100, f"{sender} -> {receiver}: the basis must arrive with the tokens"
        assert arrival.basis_observed_delta == 100
        assert states[receiver].cost_basis_native == 0, "it is then released by the sale"
        assert states[receiver].realized_pnl_native == 50, "150 received against the 100 the sender paid"
        assert states[receiver].unresolved_tokens == 0
        assert states[sender].cost_basis_native == 0 and states[sender].observed_tokens == 0
        assert states[sender].realized_pnl_native == 0, "handing tokens away is not a gain"


def test_05b_an_unresolved_sender_hands_on_nothing_to_inherit():
    from core.ledger.fold import fold_token

    flows = [
        fold_flow(KIND_TRANSFER_IN, 100, None, block=1, wallet=SENDER_LOW, basis_state=BASIS_UNRESOLVED),
        *_transfer_pair(SENDER_LOW, RECEIVER_MID, block=2, log_index=4, amount=100),
    ]
    states, out = fold_token(None, flows)
    arrival = out[-1]
    assert arrival.basis_delta == 0
    assert states[RECEIVER_MID].unresolved_tokens == 100
