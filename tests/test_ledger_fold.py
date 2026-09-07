import random
from decimal import Decimal

from core.ledger.fold import PARKED_AGGREGATES, Parked, PositionState, fold, fold_position
from core.ledger.types import Flow

WALLET = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
TOKEN = "0x8e74f6e943a7a28605ddd59945bec63a8919f5e2"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
USDC = "0x754704bc059f8c67012fed69bc8a327a5aafb603"


def _flow(
    kind,
    token_delta,
    quote=None,
    basis_state="observed",
    block=1,
    log_index=0,
    sub_index=0,
    tx_index=0,
    txhash=None,
    timestamp=None,
    quote_asset="native",
    mon_value=None,
    source="transfer_net",
    counterparty=None,
    wallet=None,
):
    if quote is None:
        quote_asset = None
    if mon_value is None:
        mon_value = Decimal(abs(quote)) / Decimal(10**18) if quote is not None else Decimal(0)
    return Flow(
        block_number=block,
        tx_index=tx_index,
        log_index=log_index,
        sub_index=sub_index,
        txhash=txhash or f"0x{block:064x}",
        timestamp=timestamp if timestamp is not None else 1_700_000_000 + block,
        wallet=wallet or WALLET,
        token=TOKEN,
        token_delta=token_delta,
        quote_asset=quote_asset,
        quote_delta=quote,
        mon_value=mon_value,
        usd_value=Decimal(0),
        kind=kind,
        venue=None,
        counterparty=counterparty,
        origin=WALLET,
        source=source,
        basis_state=basis_state,
        price_native=None,
        basis_delta=0,
        realized_delta=0,
    )


def _closed_identity(state, carried_out=0):
    assert state.cost_basis_native == 0
    assert state.basis_estimated_native == 0
    assert state.observed_tokens == 0
    assert state.estimated_tokens == 0
    assert state.unresolved_tokens == 0
    assert (
        state.realized_pnl_native
        + state.realized_estimated_native
        + state.unresolved_proceeds_native
        - carried_out
        - state.disposed_unresolved_basis_native
        == state.native_received - state.native_spent
    )


def _carried_out(out):
    return -sum(f.basis_delta for f in out if f.kind == "transfer_out")


def test_buy_sell_buy_sell_average_cost():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("sell", -50, 800, block=2),
        _flow("buy", 100, -3000, block=3),
        _flow("sell", -150, 4000, block=4),
    ]
    state, out = fold(None, flows)
    assert out[0].basis_delta == 1000 and out[0].realized_delta == 0
    assert out[1].basis_delta == -500 and out[1].realized_delta == 300
    assert out[2].basis_delta == 3000 and out[2].realized_delta == 0
    assert out[3].basis_delta == -3500 and out[3].realized_delta == 500
    assert state.realized_pnl_native == 800
    assert state.native_spent == 4000 and state.native_received == 4800
    assert state.token_bought == 200 and state.token_sold == 200
    assert state.balance_token == 0
    assert state.trade_count == 4 and state.buy_count == 2 and state.sell_count == 2
    _closed_identity(state)


def test_settler_sell_with_observed_basis():
    flows = [
        _flow("buy", 200, -1000, block=1),
        _flow("sell", -200, 1500, block=2, quote_asset=WMON, source="venue_event"),
    ]
    state, out = fold(None, flows)
    assert out[1].basis_delta == -1000 and out[1].realized_delta == 500
    assert state.realized_pnl_native == 500
    assert state.realized_estimated_native == 0
    assert state.unresolved_proceeds_native == 0
    assert state.trade_count == 2 and state.sell_count == 1
    _closed_identity(state)


def test_sale_of_unresolved_tokens_books_proceeds_without_gain():
    flows = [
        _flow("transfer_in", 100, basis_state="unresolved", block=1),
        _flow("sell", -100, 900, block=2),
    ]
    state, out = fold(None, flows)
    assert out[0].basis_delta == 0 and out[0].realized_delta == 0
    assert out[1].basis_delta == 0 and out[1].realized_delta == 0
    assert state.unresolved_proceeds_native == 900
    assert state.realized_pnl_native == 0
    assert state.realized_estimated_native == 0
    assert state.unresolved_tokens == 0
    assert state.native_received == 900
    assert state.token_sold == 100
    assert state.balance_token == 0
    _closed_identity(state)


def test_transfer_out_then_sale_of_remaining():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("transfer_out", -40, block=2),
        _flow("sell", -60, 900, block=3),
    ]
    state, out = fold(None, flows)
    assert out[1].basis_delta == -400 and out[1].realized_delta == 0
    assert out[2].basis_delta == -600 and out[2].realized_delta == 300
    assert state.realized_pnl_native == 300
    assert state.cost_basis_native == 0
    assert state.balance_token == 0
    assert state.token_sold == 60
    assert state.trade_count == 2


def test_custody_deposit_and_withdraw_leave_basis_untouched():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("custody_deposit", -70, block=2),
    ]
    state, out = fold(None, flows)
    assert state.balance_token == 30 and state.custody_balance == 70
    assert state.cost_basis_native == 1000
    assert out[1].basis_delta == 0 and out[1].realized_delta == 0
    state, out = fold(state, [_flow("custody_withdraw", 70, block=3)])
    assert state.balance_token == 100 and state.custody_balance == 0
    assert state.cost_basis_native == 1000
    assert out[0].basis_delta == 0
    assert state.trade_count == 1 and state.flow_count == 3


def test_sell_releases_across_observed_and_estimated_buckets():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("buy", 100, -2000, block=2, basis_state="estimated"),
        _flow("sell", -100, 3000, block=3),
    ]
    state, out = fold(None, flows)
    assert out[2].basis_delta == -1500 and out[2].realized_delta == 1500
    assert state.realized_pnl_native == 1000
    assert state.realized_estimated_native == 500
    assert state.cost_basis_native == 500
    assert state.basis_estimated_native == 1000
    assert state.observed_tokens == 50 and state.estimated_tokens == 50


def test_estimated_sell_books_realized_as_estimated():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("sell", -100, 1200, block=2, basis_state="estimated"),
    ]
    state, _ = fold(None, flows)
    assert state.realized_pnl_native == 0
    assert state.realized_estimated_native == 200
    _closed_identity(state)


def test_lp_add_parks_basis_and_lp_remove_restores_pro_rata():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("lp_add", -100, block=2),
        _flow("lp_remove", 50, block=3),
        _flow("vault_deposit", -50, block=4),
        _flow("vault_withdraw", 100, block=5),
    ]
    state, out = fold(None, flows)
    assert out[1].basis_delta == -1000
    assert out[2].basis_delta == 500
    assert out[3].basis_delta == -500
    assert out[4].basis_delta == 1000
    assert state.cost_basis_native == 1000
    assert state.parked_tokens == 0
    assert state.balance_token == 100
    assert state.observed_tokens == 100


def test_lp_remove_beyond_parked_is_unresolved():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("lp_add", -100, block=2),
        _flow("lp_remove", 120, block=3),
    ]
    state, _ = fold(None, flows)
    assert state.cost_basis_native == 1000
    assert state.observed_tokens == 100
    assert state.unresolved_tokens == 20


def test_burn_realizes_at_zero_proceeds():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("burn", -100, block=2),
    ]
    state, out = fold(None, flows)
    assert out[1].basis_delta == -1000 and out[1].realized_delta == -1000
    assert state.realized_pnl_native == -1000
    assert state.token_sold == 0
    _closed_identity(state)


def test_swap_legs_value_at_mon_value():
    flows = [
        _flow("swap_leg", 100, block=1, mon_value=Decimal("0.5"), basis_state="estimated"),
        _flow("swap_leg", -100, block=2, mon_value=Decimal("0.75"), basis_state="estimated"),
    ]
    state, out = fold(None, flows)
    assert out[0].basis_delta == 5 * 10**17
    assert out[1].basis_delta == -(5 * 10**17) and out[1].realized_delta == 25 * 10**16
    assert state.native_spent == 5 * 10**17 and state.native_received == 75 * 10**16
    assert state.realized_estimated_native == 25 * 10**16
    assert state.trade_count == 2
    _closed_identity(state)


def test_usdc_quoted_trade_uses_mon_value():
    flows = [
        _flow("buy", 100, -5_000_000, block=1, quote_asset=USDC, mon_value=Decimal("2")),
        _flow("sell", -100, 6_000_000, block=2, quote_asset=USDC, mon_value=Decimal("3")),
    ]
    state, _ = fold(None, flows)
    assert state.native_spent == 2 * 10**18
    assert state.native_received == 3 * 10**18
    assert state.realized_pnl_native == 10**18


def test_trade_counts_are_distinct_transactions():
    tx = "0xabc"
    flows = [
        _flow("buy", 100, -1000, block=1, log_index=3, txhash=tx),
        _flow("buy", 50, -500, block=1, log_index=7, txhash=tx),
        _flow("sell", -150, 2000, block=2, log_index=1),
    ]
    state, _ = fold(None, flows)
    assert state.trade_count == 2 and state.buy_count == 1 and state.sell_count == 1
    assert state.flow_count == 3


def test_mint_without_payment_and_airdrop_are_unresolved():
    flows = [
        _flow("mint", 100, block=1),
        _flow("airdrop", 50, block=2, basis_state="unresolved"),
        _flow("mint", 10, -100, block=3),
    ]
    state, _ = fold(None, flows)
    assert state.unresolved_tokens == 150
    assert state.observed_tokens == 10
    assert state.token_bought == 10
    assert state.trade_count == 1


def test_flows_are_folded_in_chain_order_and_timestamps_tracked():
    flows = [
        _flow("sell", -100, 1500, block=5, tx_index=2, timestamp=200),
        _flow("buy", 100, -1000, block=5, tx_index=1, timestamp=200),
        _flow("buy", 10, -50, block=2, timestamp=100),
    ]
    state, out = fold(None, flows)
    assert [f.block_number for f in out] == [2, 5, 5]
    assert out[1].kind == "buy" and out[2].kind == "sell"
    assert state.first_flow_ts == 100 and state.last_flow_ts == 200 and state.last_flow_block == 5
    assert state.realized_pnl_native == 1500 - (1050 * 100 // 110)
    assert state.wallet == WALLET and state.token == TOKEN


def test_incremental_fold_matches_full_fold():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("sell", -30, 600, block=2),
        _flow("buy", 20, -900, block=3),
        _flow("sell", -90, 4000, block=4),
    ]
    full, _ = fold(None, flows)
    first, _ = fold(None, flows[:2])
    second, _ = fold(first, flows[2:])
    assert first.trade_count == 2
    assert second.to_row() == full.to_row()


def test_fold_position_returns_row():
    row, out = fold_position(WALLET, TOKEN, [_flow("buy", 100, -1000, block=1)])
    assert row.wallet == WALLET and row.token == TOKEN
    assert row.cost_basis_native == 1000 and row.balance_token == 100
    assert len(out) == 1
    assert isinstance(PositionState().to_row(), type(row))


def _random_sequence(rng, allow_unresolved):
    held = 0
    parked = 0
    custody = 0
    flows = []
    block = 0

    def add(kind, delta, quote=None, **kw):
        nonlocal block
        block += 1
        flows.append(_flow(kind, delta, quote, block=block, **kw))

    for _ in range(rng.randint(5, 40)):
        choice = rng.random()
        if choice < 0.3 or held == 0 and parked == 0 and custody == 0:
            amount = rng.randint(1, 10**6)
            basis_state = "estimated" if rng.random() < 0.2 else "observed"
            add("buy", amount, -rng.randint(1, 10**9), basis_state=basis_state)
            held += amount
        elif choice < 0.5 and held > 0:
            amount = rng.randint(1, held)
            basis_state = "estimated" if rng.random() < 0.1 else "observed"
            add("sell", -amount, rng.randint(0, 10**9), basis_state=basis_state)
            held -= amount
        elif choice < 0.58 and held > 0:
            amount = rng.randint(1, held)
            add("transfer_out", -amount)
            held -= amount
        elif choice < 0.64 and allow_unresolved:
            amount = rng.randint(1, 10**6)
            add("transfer_in", amount, basis_state="unresolved")
            held += amount
        elif choice < 0.7 and held > 0:
            amount = rng.randint(1, held)
            add("burn", -amount)
            held -= amount
        elif choice < 0.78 and held > 0:
            amount = rng.randint(1, held)
            add(rng.choice(["lp_add", "vault_deposit"]), -amount)
            held -= amount
            parked += amount
        elif choice < 0.86 and parked > 0:
            amount = rng.randint(1, parked)
            add(rng.choice(["lp_remove", "vault_withdraw"]), amount)
            held += amount
            parked -= amount
        elif choice < 0.93 and held > 0:
            amount = rng.randint(1, held)
            add("custody_deposit", -amount)
            held -= amount
            custody += amount
        elif custody > 0:
            amount = rng.randint(1, custody)
            add("custody_withdraw", amount)
            held += amount
            custody -= amount
    if custody:
        add("custody_withdraw", custody)
        held += custody
    if parked:
        add("lp_remove", parked)
        held += parked
    if held:
        add("sell", -held, rng.randint(0, 10**9))
    return flows


def test_closed_position_property_without_unresolved():
    for seed in range(200):
        rng = random.Random(seed)
        flows = _random_sequence(rng, allow_unresolved=False)
        state, out = fold(None, flows)
        carried_out = _carried_out(out)
        assert state.balance_token == 0 and state.custody_balance == 0 and state.parked_tokens == 0
        assert state.cost_basis_native == 0 and state.basis_estimated_native == 0
        assert state.unresolved_proceeds_native == 0
        assert (
            state.realized_pnl_native
            + state.realized_estimated_native
            - carried_out
            - state.disposed_unresolved_basis_native
            == state.native_received - state.native_spent
        ), seed
        assert sum(f.basis_delta for f in out) == 0
        assert sum(f.realized_delta for f in out) == state.realized_pnl_native + state.realized_estimated_native


def test_closed_position_property_with_unresolved():
    for seed in range(200):
        rng = random.Random(1000 + seed)
        flows = _random_sequence(rng, allow_unresolved=True)
        state, out = fold(None, flows)
        carried_out = _carried_out(out)
        assert state.balance_token == 0 and state.parked_tokens == 0
        _closed_identity(state, carried_out)
        assert sum(f.basis_delta for f in out) == 0
        assert sum(f.realized_delta for f in out) == state.realized_pnl_native + state.realized_estimated_native


def test_basis_never_negative_during_random_sequence():
    for seed in range(100):
        rng = random.Random(5000 + seed)
        flows = _random_sequence(rng, allow_unresolved=True)
        state = None
        for flow in flows:
            state, _ = fold(state, [flow])
            assert state.cost_basis_native >= 0 and state.basis_estimated_native >= 0
            assert state.observed_tokens >= 0 and state.estimated_tokens >= 0 and state.unresolved_tokens >= 0
            assert state.parked_observed_basis >= 0 and state.parked_estimated_basis >= 0


def test_nothing_the_fold_carries_is_lost_on_the_way_to_storage():
    """Every working field is persisted: the scalars in the position row, the per-venue buckets beside it."""
    from dataclasses import fields

    from core.ledger.store import PARKED_COLUMNS
    from core.ledger.types import PositionRow

    state_fields = {f.name for f in fields(PositionState)}
    row_fields = {f.name for f in fields(PositionRow)}
    assert row_fields <= state_fields | set(PARKED_AGGREGATES)
    for name in row_fields:
        assert hasattr(PositionState(), name), name
    assert state_fields - row_fields == {"parked"}
    assert {f.name for f in fields(Parked)} == set(PARKED_COLUMNS)


PARKING = ("lp_add", "lp_remove", "vault_deposit", "vault_withdraw")


def test_flow_effects_sum_to_the_position_inventory():
    for seed in range(200):
        rng = random.Random(9000 + seed)
        flows = _random_sequence(rng, allow_unresolved=True)
        state, out = fold(None, flows)
        assert sum(f.qty_observed for f in out) == state.observed_tokens, seed
        assert sum(f.qty_estimated for f in out) == state.estimated_tokens, seed
        assert sum(f.qty_unresolved for f in out) == state.unresolved_tokens, seed
        assert sum(f.basis_observed_delta for f in out) == state.cost_basis_native, seed
        assert sum(f.basis_estimated_delta for f in out) == state.basis_estimated_native, seed
        assert sum(f.realized_observed_delta for f in out) == state.realized_pnl_native, seed
        assert sum(f.realized_estimated_delta for f in out) == state.realized_estimated_native, seed
        assert sum(f.unresolved_proceeds_delta for f in out) == state.unresolved_proceeds_native, seed
        assert sum(f.disposed_unresolved_basis_delta for f in out) == state.disposed_unresolved_basis_native, seed
        parked = [f for f in out if f.kind in PARKING]
        assert -sum(f.qty_observed for f in parked) == state.parked_observed_tokens, seed
        assert -sum(f.qty_estimated for f in parked) == state.parked_estimated_tokens, seed
        assert -sum(f.basis_observed_delta for f in parked) == state.parked_observed_basis, seed
        assert -sum(f.basis_estimated_delta for f in parked) == state.parked_estimated_basis, seed
        for f in out:
            assert f.basis_delta == f.basis_observed_delta + f.basis_estimated_delta
            assert f.realized_delta == f.realized_observed_delta + f.realized_estimated_delta


def test_fold_resumes_from_a_stored_row_at_any_cut():
    from core.ledger.types import PositionRow

    for seed in range(200):
        rng = random.Random(12000 + seed)
        flows = _random_sequence(rng, allow_unresolved=True)
        whole, whole_out = fold(None, flows)
        cut = rng.randint(0, len(flows))
        head, head_out = fold(None, flows[:cut])
        row = head.to_row()
        assert isinstance(row, PositionRow)
        tail, tail_out = fold(PositionState.from_row(row, head.parked), flows[cut:])
        assert tail.to_row() == whole.to_row(), (seed, cut)
        assert tail.parked == whole.parked, (seed, cut)
        assert head_out + tail_out == whole_out, (seed, cut)


def test_parking_into_two_places_keeps_each_place_at_its_own_cost():
    a, b = "0x" + "a5" * 20, "0x" + "b6" * 20
    flows = [
        _flow("buy", 100, -100, block=1),
        _flow("vault_deposit", -100, block=2, counterparty=a),
        _flow("buy", 100, -1000, block=3),
        _flow("vault_deposit", -100, block=4, counterparty=b),
        _flow("vault_withdraw", 100, block=5, counterparty=a),
        _flow("vault_withdraw", 100, block=6, counterparty=b),
    ]
    state, out = fold(None, flows)
    assert out[4].basis_delta == 100 and out[5].basis_delta == 1000
    assert state.cost_basis_native == 1100
    assert state.parked == {}
    assert state.observed_tokens == 200


def test_a_partial_withdrawal_takes_a_share_of_only_that_places_basis():
    a, b = "0x" + "a5" * 20, "0x" + "b6" * 20
    flows = [
        _flow("buy", 200, -400, block=1),
        _flow("lp_add", -100, block=2, counterparty=a),
        _flow("lp_add", -100, block=3, counterparty=b),
        _flow("lp_remove", 40, block=4, counterparty=a),
    ]
    state, out = fold(None, flows)
    assert out[3].basis_delta == 80
    assert state.parked[a].observed_basis == 120 and state.parked[a].observed_tokens == 60
    assert state.parked[b].observed_basis == 200 and state.parked[b].observed_tokens == 100


def test_an_unpriced_sale_is_neither_a_loss_nor_part_of_the_running_average():
    flows = [
        _flow("buy", 100, -1000, block=1),
        _flow("sell", -50, None, block=2, basis_state="unresolved"),
        _flow("sell", -50, 900, block=3),
    ]
    state, out = fold(None, flows)
    assert out[1].basis_delta == -500 and out[1].realized_delta == 0
    assert out[1].disposed_unresolved_basis_delta == 500
    assert state.disposed_unresolved_tokens == 50 and state.disposed_unresolved_basis_native == 500
    assert out[2].basis_delta == -500 and out[2].realized_delta == 400
    assert state.realized_pnl_native == 400 and state.realized_estimated_native == 0
    assert state.trade_count == 3 and state.sell_count == 2
    _closed_identity(state)


WALLETS = ["0x" + f"{n:02x}" * 20 for n in (0x11, 0x55, 0x99, 0xDD)]


def _random_token_history(rng):
    """Several wallets trading one token, handing it to each other, over random blocks."""
    held = dict.fromkeys(WALLETS, 0)
    flows = []
    block = 0
    for _ in range(rng.randint(8, 60)):
        block += 1
        actor = rng.choice(WALLETS)
        choice = rng.random()
        owners = [w for w in WALLETS if held[w] > 0]
        if choice < 0.35 or not owners:
            amount = rng.randint(1, 10**6)
            flows.append(_flow("buy", amount, -rng.randint(1, 10**9), block=block, wallet=actor))
            held[actor] += amount
        elif choice < 0.6:
            seller = rng.choice(owners)
            amount = rng.randint(1, held[seller])
            flows.append(_flow("sell", -amount, rng.randint(0, 10**9), block=block, wallet=seller))
            held[seller] -= amount
        else:
            sender = rng.choice(owners)
            receiver = rng.choice([w for w in WALLETS if w != sender])
            amount = rng.randint(1, held[sender])
            log_index = rng.randint(0, 5)
            flows += [
                _flow(
                    "transfer_out",
                    -amount,
                    block=block,
                    log_index=log_index,
                    sub_index=0,
                    wallet=sender,
                    counterparty=receiver,
                ),
                _flow(
                    "transfer_in",
                    amount,
                    block=block,
                    log_index=log_index,
                    sub_index=1,
                    wallet=receiver,
                    counterparty=sender,
                ),
            ]
            held[sender] -= amount
            held[receiver] += amount
    return flows


def _transfer_pairs(out):
    pairs = {}
    for flow in out:
        if flow.kind in ("transfer_out", "transfer_in"):
            pairs.setdefault((flow.txhash, flow.log_index), []).append(flow)
    return [legs for legs in pairs.values() if len(legs) == 2]


def test_a_transfer_moves_cost_between_wallets_without_creating_or_destroying_any():
    from core.ledger.fold import fold_token

    for seed in range(200):
        rng = random.Random(20000 + seed)
        flows = _random_token_history(rng)
        states, out = fold_token(None, flows)
        for legs in _transfer_pairs(out):
            gone = next(f for f in legs if f.token_delta < 0)
            arrived = next(f for f in legs if f.token_delta > 0)
            assert arrived.basis_observed_delta == -gone.basis_observed_delta, seed
            assert arrived.basis_estimated_delta == -gone.basis_estimated_delta, seed
            assert arrived.qty_observed == -gone.qty_observed, seed
            assert arrived.qty_unresolved == -gone.qty_unresolved, seed
        assert sum(f.basis_observed_delta for f in out) == sum(s.cost_basis_native for s in states.values()), seed
        assert sum(f.qty_observed for f in out) == sum(s.observed_tokens for s in states.values()), seed


def test_a_token_folded_in_pieces_lands_where_one_pass_lands():
    from core.ledger.fold import fold_token

    for seed in range(200):
        rng = random.Random(30000 + seed)
        flows = _random_token_history(rng)
        whole, whole_out = fold_token(None, flows)
        blocks = sorted({f.block_number for f in flows})
        cut = rng.choice(blocks)
        head, head_out = fold_token(None, [f for f in flows if f.block_number <= cut])
        resumed = {w: PositionState.from_row(s.to_row(), s.parked) for w, s in head.items()}
        tail, tail_out = fold_token(resumed, [f for f in flows if f.block_number > cut])
        assert {w: s.to_row() for w, s in tail.items()} == {w: s.to_row() for w, s in whole.items()}, (seed, cut)
        assert sorted(head_out + tail_out, key=lambda f: (f.block_number, f.log_index, f.sub_index)) == sorted(
            whole_out, key=lambda f: (f.block_number, f.log_index, f.sub_index)
        ), (seed, cut)


def test_an_inbound_flow_the_netting_could_not_stand_behind_still_inherits():
    """A price the netting marked unresolved is not evidence, so the sender's basis is still the better answer."""
    from core.ledger.fold import fold_token

    sender, receiver = WALLETS[0], WALLETS[1]
    flows = [
        _flow("buy", 100, -100, block=1, wallet=sender),
        _flow("transfer_out", -100, block=2, log_index=4, sub_index=0, wallet=sender, counterparty=receiver),
        _flow(
            "transfer_in",
            100,
            999,
            block=2,
            log_index=4,
            sub_index=1,
            wallet=receiver,
            counterparty=sender,
            basis_state="unresolved",
        ),
    ]
    states, out = fold_token(None, flows)
    assert out[-1].basis_delta == 100, "the discarded price must not block the inheritance"
    assert states[receiver].cost_basis_native == 100
    assert states[receiver].observed_tokens == 100
