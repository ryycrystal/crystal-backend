from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from core.ledger.types import (
    BASIS_ESTIMATED,
    BASIS_OBSERVED,
    BASIS_UNRESOLVED,
    KIND_AIRDROP,
    KIND_BURN,
    KIND_BUY,
    KIND_CUSTODY_DEPOSIT,
    KIND_CUSTODY_WITHDRAW,
    KIND_LP_ADD,
    KIND_LP_REMOVE,
    KIND_MINT,
    KIND_SELL,
    KIND_SWAP_LEG,
    KIND_TRANSFER_IN,
    KIND_TRANSFER_OUT,
    KIND_VAULT_DEPOSIT,
    KIND_VAULT_WITHDRAW,
    MON_FAMILY,
    WEI,
    Effect,
    Flow,
    PositionRow,
)

BUY_KINDS = frozenset({KIND_BUY, KIND_MINT})
INBOUND_KINDS = frozenset({KIND_TRANSFER_IN, KIND_AIRDROP})
PARK_KINDS = frozenset({KIND_LP_ADD, KIND_VAULT_DEPOSIT})
RESTORE_KINDS = frozenset({KIND_LP_REMOVE, KIND_VAULT_WITHDRAW})
PARKED_AGGREGATES = (
    "parked_observed_tokens",
    "parked_estimated_tokens",
    "parked_unresolved_tokens",
    "parked_observed_basis",
    "parked_estimated_basis",
)


@dataclass
class Parked:
    """What one pool or vault holds of this position, and at what cost.

    Basis has to remember where it went. Pooled into a single aggregate per wallet and token, a withdrawal
    from a cheap vault returns the average of every vault the wallet ever used, which is a gain or a loss
    the wallet never had.
    """

    observed_tokens: int = 0
    estimated_tokens: int = 0
    unresolved_tokens: int = 0
    observed_basis: int = 0
    estimated_basis: int = 0

    @property
    def empty(self) -> bool:
        return not (self.observed_tokens or self.estimated_tokens or self.unresolved_tokens)


@dataclass
class PositionState:
    """The fold's working record.

    Every field here is persisted, so a stored row plus its parked rows is a checkpoint: folding new flows
    onto a position read back from the database gives exactly what folding the whole history would. The
    transaction counters rely on one transaction's flows being contiguous in chain order, which holds for a
    single wallet and token because they share a block and a transaction index.
    """

    wallet: str = ""
    token: str = ""
    balance_token: int = 0
    custody_balance: int = 0
    token_bought: int = 0
    token_sold: int = 0
    native_spent: int = 0
    native_received: int = 0
    cost_basis_native: int = 0
    realized_pnl_native: int = 0
    basis_estimated_native: int = 0
    realized_estimated_native: int = 0
    unresolved_tokens: int = 0
    unresolved_proceeds_native: int = 0
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    first_flow_ts: int | None = None
    last_flow_ts: int | None = None
    last_flow_block: int | None = None
    flow_count: int = 0
    observed_tokens: int = 0
    estimated_tokens: int = 0
    disposed_unresolved_tokens: int = 0
    disposed_unresolved_basis_native: int = 0
    last_trade_tx: str | None = None
    last_buy_tx: str | None = None
    last_sell_tx: str | None = None
    parked: dict[str, Parked] = field(default_factory=dict)

    def to_row(self) -> PositionRow:
        return PositionRow(**{name: getattr(self, name) for name in PositionRow.__dataclass_fields__})

    @classmethod
    def from_row(cls, row: PositionRow, parked: dict[str, Parked] | None = None) -> PositionState:
        values = {
            name: getattr(row, name) for name in PositionRow.__dataclass_fields__ if name not in PARKED_AGGREGATES
        }
        return cls(**values, parked={name: replace(bucket) for name, bucket in (parked or {}).items()})

    def _parked_total(self, attribute: str) -> int:
        return sum(getattr(bucket, attribute) for bucket in self.parked.values())

    @property
    def parked_observed_tokens(self) -> int:
        return self._parked_total("observed_tokens")

    @property
    def parked_estimated_tokens(self) -> int:
        return self._parked_total("estimated_tokens")

    @property
    def parked_unresolved_tokens(self) -> int:
        return self._parked_total("unresolved_tokens")

    @property
    def parked_observed_basis(self) -> int:
        return self._parked_total("observed_basis")

    @property
    def parked_estimated_basis(self) -> int:
        return self._parked_total("estimated_basis")

    @property
    def open_tokens(self) -> int:
        return self.observed_tokens + self.estimated_tokens + self.unresolved_tokens

    @property
    def parked_tokens(self) -> int:
        return self.parked_observed_tokens + self.parked_estimated_tokens + self.parked_unresolved_tokens


def _wei(value) -> int:
    if value is None:
        return 0
    return int(value)


def _quote_wei(flow: Flow) -> int:
    if flow.quote_delta is not None and flow.quote_asset in MON_FAMILY:
        return abs(_wei(flow.quote_delta))
    if flow.mon_value is None:
        return 0
    return abs(int(Decimal(flow.mon_value) * WEI))


def _apportion(amount: int, weights: list[int]) -> list[int]:
    total = sum(weights)
    if amount <= 0 or total <= 0:
        return [0] * len(weights)
    shares = [amount * w // total for w in weights]
    leftover = amount - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (amount * weights[i]) % total, reverse=True)
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def _split(amount: int, parts: list[int]) -> list[int]:
    if amount >= sum(parts):
        return list(parts)
    return _apportion(amount, parts)


def _release(basis: int, taken: int, held: int) -> int:
    if held <= 0 or taken <= 0:
        return 0
    if taken >= held:
        return basis
    return basis * taken // held


def _take_open(state: PositionState, amount: int) -> tuple[list[int], int, int]:
    taken = _split(amount, [state.observed_tokens, state.estimated_tokens, state.unresolved_tokens])
    released_observed = _release(state.cost_basis_native, taken[0], state.observed_tokens)
    released_estimated = _release(state.basis_estimated_native, taken[1], state.estimated_tokens)
    state.observed_tokens -= taken[0]
    state.estimated_tokens -= taken[1]
    state.unresolved_tokens -= taken[2]
    state.cost_basis_native -= released_observed
    state.basis_estimated_native -= released_estimated
    return taken, released_observed, released_estimated


def _released(taken: list[int], released_observed: int, released_estimated: int) -> Effect:
    return Effect(
        qty_observed=-taken[0],
        qty_estimated=-taken[1],
        qty_unresolved=-taken[2],
        basis_observed_delta=-released_observed,
        basis_estimated_delta=-released_estimated,
    )


def _open(state: PositionState, amount: int, cost: int, basis_state: str) -> Effect:
    if basis_state == BASIS_OBSERVED:
        state.observed_tokens += amount
        state.cost_basis_native += cost
        return Effect(qty_observed=amount, basis_observed_delta=cost)
    if basis_state == BASIS_ESTIMATED:
        state.estimated_tokens += amount
        state.basis_estimated_native += cost
        return Effect(qty_estimated=amount, basis_estimated_delta=cost)
    state.unresolved_tokens += amount
    return Effect(qty_unresolved=amount)


def _record_trade(state: PositionState, flow: Flow, buy: bool) -> None:
    if flow.txhash != state.last_trade_tx:
        state.trade_count += 1
        state.last_trade_tx = flow.txhash
    if buy:
        if flow.txhash != state.last_buy_tx:
            state.buy_count += 1
            state.last_buy_tx = flow.txhash
    elif flow.txhash != state.last_sell_tx:
        state.sell_count += 1
        state.last_sell_tx = flow.txhash


def _apply_buy(state: PositionState, flow: Flow, amount: int) -> Effect:
    cost = _quote_wei(flow)
    basis_state = flow.basis_state if cost > 0 or flow.basis_state == BASIS_ESTIMATED else BASIS_UNRESOLVED
    if basis_state == BASIS_UNRESOLVED:
        state.unresolved_tokens += amount
        return Effect(qty_unresolved=amount)
    state.token_bought += amount
    state.native_spent += cost
    _record_trade(state, flow, buy=True)
    return _open(state, amount, cost, basis_state)


def _apply_sell(state: PositionState, flow: Flow, amount: int) -> Effect:
    """A sale draws on every inventory state in proportion and splits its proceeds the same way.

    When the proceeds are unknown the released basis is neither a loss nor a gain: it leaves the running
    average, so later sales are priced on what remains, and waits in its own bucket until the proceeds are
    learned. Booking it as a loss was how an unpriced disposal used to read as a realized loss of the whole
    cost.
    """
    proceeds = _quote_wei(flow)
    state.token_sold += amount
    state.native_received += proceeds
    _record_trade(state, flow, buy=False)
    taken, released_observed, released_estimated = _take_open(state, amount)
    effect = _released(taken, released_observed, released_estimated)
    if proceeds <= 0:
        held_basis = released_observed + released_estimated
        state.disposed_unresolved_tokens += sum(taken)
        state.disposed_unresolved_basis_native += held_basis
        effect.disposed_unresolved_basis_delta = held_basis
        return effect
    excess = amount - sum(taken)
    shares = _apportion(proceeds, taken + [excess])
    gain_observed = shares[0] - released_observed
    gain_estimated = shares[1] - released_estimated
    effect.unresolved_proceeds_delta = shares[2] + shares[3]
    if flow.basis_state == BASIS_OBSERVED:
        effect.realized_observed_delta = gain_observed
        effect.realized_estimated_delta = gain_estimated
    else:
        effect.realized_estimated_delta = gain_observed + gain_estimated
    state.unresolved_proceeds_native += effect.unresolved_proceeds_delta
    state.realized_pnl_native += effect.realized_observed_delta
    state.realized_estimated_native += effect.realized_estimated_delta
    return effect


def _handover(flow: Flow) -> tuple[str, int]:
    """One transfer is one log, so its two halves name each other by their shared chain position."""
    return (flow.txhash, flow.log_index)


def _apply_transfer_out(state: PositionState, flow: Flow, amount: int, transit: dict | None) -> Effect:
    taken, released_observed, released_estimated = _take_open(state, amount)
    effect = _released(taken, released_observed, released_estimated)
    if transit is not None:
        transit[_handover(flow)] = effect
    return effect


def _apply_transfer_in(state: PositionState, flow: Flow, amount: int, transit: dict | None) -> Effect:
    """Tokens arrive at the cost the sender released, unless the receiver paid a price of its own.

    Without this a transfer destroys cost: the sender's basis leaves and nothing takes it up, so the
    receiver holds tokens at no known price and their eventual sale reads as pure profit. What the receiver
    actually paid, when there is such a payment the netting could stand behind, is better evidence than the
    sender's history and wins.

    The row's own `basis_state` still describes this movement's own price, which for a plain transfer is
    nothing. The confidence of what the receiver now holds is in the effect vector, not that label.

    A sender that released real basis wins over the receiver's own quote, because the two halves of one
    transfer cannot be a gift on one side and a purchase on the other. Letting a stray dust quote win
    instead destroyed 47,875 MON at a single hand-off on moncock. Where the sender released nothing, there
    is nothing to conserve and whatever the receiver paid is the better evidence.
    """
    handed_over = transit.pop(_handover(flow), None) if transit is not None else None
    carried = -handed_over.basis_delta if handed_over is not None else 0
    cost = _quote_wei(flow)
    if carried <= 0 and cost > 0 and flow.basis_state != BASIS_UNRESOLVED:
        return _open(state, amount, cost, flow.basis_state)
    if handed_over is None:
        return _open(state, amount, 0, BASIS_UNRESOLVED)
    arriving = -handed_over
    effect = Effect()
    for quantity, basis, basis_state in (
        (arriving.qty_observed, arriving.basis_observed_delta, BASIS_OBSERVED),
        (arriving.qty_estimated, arriving.basis_estimated_delta, BASIS_ESTIMATED),
        (arriving.qty_unresolved, 0, BASIS_UNRESOLVED),
    ):
        if quantity:
            effect = effect + _open(state, quantity, basis, basis_state)
    short = amount - (arriving.qty_observed + arriving.qty_estimated + arriving.qty_unresolved)
    if short > 0:
        effect = effect + _open(state, short, 0, BASIS_UNRESOLVED)
    return effect


def _apply_burn(state: PositionState, amount: int) -> Effect:
    taken, released_observed, released_estimated = _take_open(state, amount)
    effect = _released(taken, released_observed, released_estimated)
    effect.realized_observed_delta = -released_observed
    effect.realized_estimated_delta = -released_estimated
    state.realized_pnl_native -= released_observed
    state.realized_estimated_native -= released_estimated
    return effect


def _destination(flow: Flow) -> str:
    """Which pool or vault the tokens went into. The counterparty names it; the venue is the fallback."""
    return (flow.counterparty or flow.venue or "").lower()


def _apply_park(state: PositionState, flow: Flow, amount: int) -> Effect:
    taken, released_observed, released_estimated = _take_open(state, amount)
    bucket = state.parked.setdefault(_destination(flow), Parked())
    bucket.observed_tokens += taken[0]
    bucket.estimated_tokens += taken[1]
    bucket.unresolved_tokens += taken[2]
    bucket.observed_basis += released_observed
    bucket.estimated_basis += released_estimated
    return _released(taken, released_observed, released_estimated)


def _apply_restore(state: PositionState, flow: Flow, amount: int) -> Effect:
    destination = _destination(flow)
    bucket = state.parked.get(destination) or Parked()
    taken = _split(amount, [bucket.observed_tokens, bucket.estimated_tokens, bucket.unresolved_tokens])
    restored_observed = _release(bucket.observed_basis, taken[0], bucket.observed_tokens)
    restored_estimated = _release(bucket.estimated_basis, taken[1], bucket.estimated_tokens)
    bucket.observed_tokens -= taken[0]
    bucket.estimated_tokens -= taken[1]
    bucket.unresolved_tokens -= taken[2]
    bucket.observed_basis -= restored_observed
    bucket.estimated_basis -= restored_estimated
    if bucket.empty:
        state.parked.pop(destination, None)
    unresolved = taken[2] + (amount - sum(taken))
    state.observed_tokens += taken[0]
    state.estimated_tokens += taken[1]
    state.unresolved_tokens += unresolved
    state.cost_basis_native += restored_observed
    state.basis_estimated_native += restored_estimated
    return Effect(
        qty_observed=taken[0],
        qty_estimated=taken[1],
        qty_unresolved=unresolved,
        basis_observed_delta=restored_observed,
        basis_estimated_delta=restored_estimated,
    )


def _apply_swap_leg(state: PositionState, flow: Flow, amount: int, delta: int) -> Effect:
    if delta > 0:
        return _apply_buy(state, flow, amount)
    return _apply_sell(state, flow, amount)


def _apply(state: PositionState, flow: Flow, transit: dict | None = None) -> Effect:
    delta = _wei(flow.token_delta)
    amount = abs(delta)
    kind = flow.kind
    state.balance_token += delta
    if kind == KIND_CUSTODY_DEPOSIT:
        state.custody_balance += amount
        return Effect()
    if kind == KIND_CUSTODY_WITHDRAW:
        state.custody_balance -= amount
        return Effect()
    if kind in BUY_KINDS:
        if transit is not None and _handover(flow) in transit:
            return _apply_transfer_in(state, flow, amount, transit)
        return _apply_buy(state, flow, amount)
    if kind == KIND_SELL:
        return _apply_sell(state, flow, amount)
    if kind == KIND_SWAP_LEG:
        return _apply_swap_leg(state, flow, amount, delta)
    if kind == KIND_TRANSFER_OUT:
        return _apply_transfer_out(state, flow, amount, transit)
    if kind in INBOUND_KINDS:
        return _apply_transfer_in(state, flow, amount, transit)
    if kind == KIND_BURN:
        return _apply_burn(state, amount)
    if kind in PARK_KINDS:
        return _apply_park(state, flow, amount)
    if kind in RESTORE_KINDS:
        return _apply_restore(state, flow, amount)
    return Effect()


def _flow_key(flow: Flow) -> tuple[int, int, int, int]:
    return (flow.block_number, flow.tx_index, flow.log_index, flow.sub_index)


def fold_token(prev: dict[str, PositionState] | None, flows: list[Flow]) -> tuple[dict[str, PositionState], list[Flow]]:
    """Fold every wallet of one token together, in chain order.

    Folding wallet by wallet cannot see a transfer as one event, only as two unrelated halves, so the cost
    the sender releases has nowhere to go and the receiver's tokens arrive priceless. One pass over the
    whole token in chain order puts both halves in the same fold, with the sending half ordered first.
    """
    states = {wallet: replace(state) for wallet, state in (prev or {}).items()}
    for state in states.values():
        state.parked = {name: replace(bucket) for name, bucket in state.parked.items()}
    out: list[Flow] = []
    transit: dict[tuple[str, int], Effect] = {}
    for flow in sorted(flows, key=_flow_key):
        state = states.get(flow.wallet)
        if state is None:
            state = states[flow.wallet] = PositionState(wallet=flow.wallet, token=flow.token)
        elif not state.wallet:
            state.wallet, state.token = flow.wallet, flow.token
        effect = _apply(state, flow, transit)
        _stamp(state, flow)
        out.append(replace(flow, **effect.as_flow_fields()))
    return states, out


def _stamp(state: PositionState, flow: Flow) -> None:
    timestamp = int(flow.timestamp)
    if state.first_flow_ts is None or timestamp < state.first_flow_ts:
        state.first_flow_ts = timestamp
    state.last_flow_ts = timestamp
    state.last_flow_block = flow.block_number
    state.flow_count += 1


def fold(prev: PositionState | None, flows: list[Flow]) -> tuple[PositionState, list[Flow]]:
    """One wallet's slice of the same fold, for callers that hold only one wallet's flows.

    A transfer's other half is by definition another wallet's, so nothing is inherited here; that is the
    whole reason the token-wide fold exists.
    """
    wallet = (prev.wallet if prev is not None else "") or next((flow.wallet for flow in flows), "")
    states, out = fold_token({wallet: prev} if prev is not None else None, flows)
    return states.get(wallet) or prev or PositionState(), out


def fold_position(wallet: str, token: str, flows: list[Flow]) -> tuple[PositionRow, list[Flow]]:
    state, out = fold(None, flows)
    state.wallet = wallet
    state.token = token
    return state.to_row(), out
