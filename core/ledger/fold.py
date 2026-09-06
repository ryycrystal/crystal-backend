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
    Flow,
    PositionRow,
)

BUY_KINDS = frozenset({KIND_BUY, KIND_MINT})
INBOUND_KINDS = frozenset({KIND_TRANSFER_IN, KIND_AIRDROP})
PARK_KINDS = frozenset({KIND_LP_ADD, KIND_VAULT_DEPOSIT})
RESTORE_KINDS = frozenset({KIND_LP_REMOVE, KIND_VAULT_WITHDRAW})


@dataclass
class PositionState:
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
    parked_observed_tokens: int = 0
    parked_estimated_tokens: int = 0
    parked_unresolved_tokens: int = 0
    parked_observed_basis: int = 0
    parked_estimated_basis: int = 0
    trade_txs: set[str] = field(default_factory=set)
    buy_txs: set[str] = field(default_factory=set)
    sell_txs: set[str] = field(default_factory=set)

    def to_row(self) -> PositionRow:
        return PositionRow(**{name: getattr(self, name) for name in PositionRow.__dataclass_fields__})

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


def _open(state: PositionState, amount: int, cost: int, basis_state: str) -> int:
    if basis_state == BASIS_OBSERVED:
        state.observed_tokens += amount
        state.cost_basis_native += cost
        return cost
    if basis_state == BASIS_ESTIMATED:
        state.estimated_tokens += amount
        state.basis_estimated_native += cost
        return cost
    state.unresolved_tokens += amount
    return 0


def _apply_buy(state: PositionState, flow: Flow, amount: int) -> tuple[int, int]:
    cost = _quote_wei(flow)
    basis_state = flow.basis_state if cost > 0 or flow.basis_state == BASIS_ESTIMATED else BASIS_UNRESOLVED
    if basis_state == BASIS_UNRESOLVED:
        state.unresolved_tokens += amount
        return 0, 0
    state.token_bought += amount
    state.native_spent += cost
    state.trade_txs.add(flow.txhash)
    state.buy_txs.add(flow.txhash)
    return _open(state, amount, cost, basis_state), 0


def _apply_sell(state: PositionState, flow: Flow, amount: int) -> tuple[int, int]:
    proceeds = _quote_wei(flow)
    state.token_sold += amount
    state.native_received += proceeds
    state.trade_txs.add(flow.txhash)
    state.sell_txs.add(flow.txhash)
    taken, released_observed, released_estimated = _take_open(state, amount)
    excess = amount - sum(taken)
    shares = _apportion(proceeds, taken + [excess])
    realized_observed = shares[0] - released_observed
    realized_estimated = shares[1] - released_estimated
    state.unresolved_proceeds_native += shares[2] + shares[3]
    if flow.basis_state == BASIS_OBSERVED:
        state.realized_pnl_native += realized_observed
        state.realized_estimated_native += realized_estimated
    else:
        state.realized_estimated_native += realized_observed + realized_estimated
    return -(released_observed + released_estimated), realized_observed + realized_estimated


def _apply_transfer_out(state: PositionState, amount: int) -> tuple[int, int]:
    _, released_observed, released_estimated = _take_open(state, amount)
    return -(released_observed + released_estimated), 0


def _apply_transfer_in(state: PositionState, flow: Flow, amount: int) -> tuple[int, int]:
    cost = _quote_wei(flow)
    basis_state = flow.basis_state if cost > 0 else BASIS_UNRESOLVED
    return _open(state, amount, cost, basis_state), 0


def _apply_burn(state: PositionState, amount: int) -> tuple[int, int]:
    _, released_observed, released_estimated = _take_open(state, amount)
    state.realized_pnl_native -= released_observed
    state.realized_estimated_native -= released_estimated
    return -(released_observed + released_estimated), -(released_observed + released_estimated)


def _apply_park(state: PositionState, amount: int) -> tuple[int, int]:
    taken, released_observed, released_estimated = _take_open(state, amount)
    state.parked_observed_tokens += taken[0]
    state.parked_estimated_tokens += taken[1]
    state.parked_unresolved_tokens += taken[2]
    state.parked_observed_basis += released_observed
    state.parked_estimated_basis += released_estimated
    return -(released_observed + released_estimated), 0


def _apply_restore(state: PositionState, amount: int) -> tuple[int, int]:
    taken = _split(
        amount, [state.parked_observed_tokens, state.parked_estimated_tokens, state.parked_unresolved_tokens]
    )
    restored_observed = _release(state.parked_observed_basis, taken[0], state.parked_observed_tokens)
    restored_estimated = _release(state.parked_estimated_basis, taken[1], state.parked_estimated_tokens)
    state.parked_observed_tokens -= taken[0]
    state.parked_estimated_tokens -= taken[1]
    state.parked_unresolved_tokens -= taken[2]
    state.parked_observed_basis -= restored_observed
    state.parked_estimated_basis -= restored_estimated
    state.observed_tokens += taken[0]
    state.estimated_tokens += taken[1]
    state.unresolved_tokens += taken[2] + (amount - sum(taken))
    state.cost_basis_native += restored_observed
    state.basis_estimated_native += restored_estimated
    return restored_observed + restored_estimated, 0


def _apply_swap_leg(state: PositionState, flow: Flow, amount: int, delta: int) -> tuple[int, int]:
    if delta > 0:
        return _apply_buy(state, flow, amount)
    return _apply_sell(state, flow, amount)


def _apply(state: PositionState, flow: Flow) -> tuple[int, int]:
    delta = _wei(flow.token_delta)
    amount = abs(delta)
    kind = flow.kind
    state.balance_token += delta
    if kind == KIND_CUSTODY_DEPOSIT:
        state.custody_balance += amount
        return 0, 0
    if kind == KIND_CUSTODY_WITHDRAW:
        state.custody_balance -= amount
        return 0, 0
    if kind in BUY_KINDS:
        return _apply_buy(state, flow, amount)
    if kind == KIND_SELL:
        return _apply_sell(state, flow, amount)
    if kind == KIND_SWAP_LEG:
        return _apply_swap_leg(state, flow, amount, delta)
    if kind == KIND_TRANSFER_OUT:
        return _apply_transfer_out(state, amount)
    if kind in INBOUND_KINDS:
        return _apply_transfer_in(state, flow, amount)
    if kind == KIND_BURN:
        return _apply_burn(state, amount)
    if kind in PARK_KINDS:
        return _apply_park(state, amount)
    if kind in RESTORE_KINDS:
        return _apply_restore(state, amount)
    return 0, 0


def _flow_key(flow: Flow) -> tuple[int, int, int, int]:
    return (flow.block_number, flow.tx_index, flow.log_index, flow.sub_index)


def _copy(prev: PositionState) -> PositionState:
    return replace(prev, trade_txs=set(prev.trade_txs), buy_txs=set(prev.buy_txs), sell_txs=set(prev.sell_txs))


def fold(prev: PositionState | None, flows: list[Flow]) -> tuple[PositionState, list[Flow]]:
    state = _copy(prev) if prev else PositionState()
    out: list[Flow] = []
    for flow in sorted(flows, key=_flow_key):
        if not state.wallet:
            state.wallet = flow.wallet
            state.token = flow.token
        basis_delta, realized_delta = _apply(state, flow)
        timestamp = int(flow.timestamp)
        if state.first_flow_ts is None or timestamp < state.first_flow_ts:
            state.first_flow_ts = timestamp
        state.last_flow_ts = timestamp
        state.last_flow_block = flow.block_number
        state.flow_count += 1
        out.append(replace(flow, basis_delta=basis_delta, realized_delta=realized_delta))
    state.trade_count = len(state.trade_txs)
    state.buy_count = len(state.buy_txs)
    state.sell_count = len(state.sell_txs)
    return state, out


def fold_position(wallet: str, token: str, flows: list[Flow]) -> tuple[PositionRow, list[Flow]]:
    state, out = fold(None, flows)
    state.wallet = wallet
    state.token = token
    return state.to_row(), out
