from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
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
    KIND_VENUE_CUSTODY,
    KIND_VENUE_POOL,
    KIND_VENUE_ROUTER,
    KIND_ZERO,
    LVMON,
    MON_FAMILY,
    NATIVE,
    QUOTE_ASSETS,
    SOURCE_RECONCILE,
    SOURCE_TRACE,
    SOURCE_TRANSFER_NET,
    SOURCE_VENUE_EVENT,
    USD_DECIMALS,
    USDC,
    VENUE_KINDS,
    WALLET_KINDS,
    WEI,
    WMON,
    ZERO,
    Flow,
    Rates,
    TokenReg,
    TxBundle,
)

CURVE_TAGS = frozenset({"LT", "NFB", "NFS"})
POOL_TAGS = frozenset({"V2SWAP", "V3SWAP", "V4SWAP"})
SETTLING_TAGS = frozenset({"V4SWAP"})
"""Venues that can move a token without an ERC-20 transfer.

Uniswap V4 settles against the pool manager's own claim balances, so a real trade can leave no transfer
at all and the event is the only evidence there is. A V2 or V3 pair has no such mechanism: it moves the
ERC-20 in the same transaction, so an event with no transfer means the tokens were sent in an earlier one
and that movement is already recorded."""
CORE_FILL_TAGS = frozenset({"TR"})
AMOUNT_TOLERANCE_WEI = 1
FEE_TOLERANCE = Decimal("0.10")
MAX_ROUTER_HOPS = 4
USD_UNIT = Decimal(10) ** USD_DECIMALS

PriceFn = Callable[[str], Decimal | None]
KindFn = Callable[[str], str]


@dataclass
class _Hint:
    log_index: int
    venue: str
    token: str
    token_delta: int
    quote_delta: int
    quote_asset: str
    user: str | None


@dataclass
class _Leg:
    wallet: str
    token: str
    token_delta: int
    log_index: int
    counterparty: str | None = None
    venue: str | None = None
    kind: str | None = None
    quote_asset: str | None = None
    quote_delta: int | None = None
    source: str = SOURCE_TRANSFER_NET
    basis_state: str = BASIS_UNRESOLVED
    hint_price: Decimal | None = None

    @property
    def resolved(self) -> bool:
        return self.quote_delta is not None


class _Kinds:
    def __init__(self, kind_of: KindFn):
        self._kind_of = kind_of
        self._cache: dict[str, str] = {}

    def of(self, addr: str) -> str:
        if addr == ZERO:
            return KIND_ZERO
        cached = self._cache.get(addr)
        if cached is None:
            cached = self._kind_of(addr) or ""
            self._cache[addr] = cached
        return cached

    def is_wallet(self, addr: str) -> bool:
        return self.of(addr) in WALLET_KINDS


def _same_sign(a: int, b: int) -> bool:
    return (a > 0) == (b > 0)


def _to_native_units(asset: str, raw: int, rates: Rates) -> int:
    if asset == LVMON:
        return int(Decimal(raw) * rates.lvmon_rate)
    return raw


def _curve_quote_asset(reg: TokenReg | None, quote_assets: frozenset[str]) -> str:
    qt = (reg.quote_token or "").lower() if reg else ""
    if qt in quote_assets and qt != NATIVE and qt != WMON:
        return qt
    return NATIVE


def _venue_quote_asset(bundle: TxBundle, venue: str, quote_assets: frozenset[str]) -> str:
    seen: dict[str, int] = defaultdict(int)
    for leg in bundle.transfers:
        if leg.token in quote_assets and venue in (leg.from_addr, leg.to_addr):
            seen[leg.token] += leg.amount
    if not seen:
        return NATIVE
    return max(seen.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _trusted_emitter(venue: str, kinds: _Kinds) -> bool:
    """A venue event is evidence only from an address that is actually a venue.

    Any contract can emit any topic. Without this, a wallet-to-wallet transfer alongside a forged swap log
    becomes an observed purchase at whatever price the forger chose, which is the strongest confidence the
    ledger has. Classification already knows which addresses are venues, so the gate costs nothing and,
    unlike the indexer's own gate, it does not expire when a contract generation is retired.
    """
    return bool(venue) and kinds.of(venue) in VENUE_KINDS


def _hints(
    bundle: TxBundle,
    tx_tokens: set[str],
    registry: dict[str, TokenReg],
    quote_assets: frozenset[str],
    kinds: _Kinds,
    markets: dict[str, tuple[str, str]] | None = None,
) -> list[_Hint]:
    out: list[_Hint] = []
    for ev in bundle.venue_events:
        parsed = ev.parsed or {}
        venue = (ev.address or "").lower()
        if not _trusted_emitter(venue, kinds):
            continue
        if ev.tag in CURVE_TAGS:
            token = (parsed.get("token") or "").lower()
            if token not in tx_tokens:
                continue
            amount_in = int(parsed.get("amount_in") or 0)
            amount_out = int(parsed.get("amount_out") or 0)
            if parsed.get("is_buy"):
                token_delta, quote_delta = amount_out, -amount_in
            else:
                token_delta, quote_delta = -amount_in, amount_out
            if token_delta == 0 or quote_delta == 0:
                continue
            user = (parsed.get("user") or "").lower() or None
            asset = _curve_quote_asset(registry.get(token), quote_assets)
            out.append(_Hint(ev.log_index, venue, token, token_delta, quote_delta, asset, user))
        elif ev.tag in CORE_FILL_TAGS:
            market = (parsed.get("market") or "").lower()
            pair = (markets or {}).get(market)
            if not pair:
                continue
            token, quote = pair[0].lower(), (pair[1] or "").lower()
            if token not in tx_tokens:
                continue
            amount_in = int(parsed.get("amount_in") or 0)
            amount_out = int(parsed.get("amount_out") or 0)
            if parsed.get("is_buy"):
                token_delta, quote_delta = amount_out, -amount_in
            else:
                token_delta, quote_delta = -amount_in, amount_out
            if token_delta == 0 or quote_delta == 0:
                continue
            user = (parsed.get("user") or "").lower() or None
            asset = quote if quote in quote_assets and quote not in (NATIVE, WMON) else NATIVE
            out.append(_Hint(ev.log_index, market, token, token_delta, quote_delta, asset, user))
        elif ev.tag in POOL_TAGS:
            sign = 1 if ev.tag == "V4SWAP" else -1
            w0 = sign * int(parsed.get("amount0") or 0)
            w1 = sign * int(parsed.get("amount1") or 0)
            if w0 == 0 or w1 == 0 or _same_sign(w0, w1):
                continue
            user = (parsed.get("user") or parsed.get("sender") or "").lower() or None
            singleton = ev.tag == "V4SWAP"
            for token in sorted(tx_tokens):
                moved = _venue_token_amounts(bundle, venue, token)
                for token_delta, quote_delta in ((w0, w1), (w1, w0)):
                    if not any(_within_fee_tolerance(amount, abs(token_delta)) for amount in moved):
                        continue
                    asset = _hint_quote_asset(bundle, venue, abs(quote_delta), quote_assets, singleton)
                    if asset is None:
                        continue
                    out.append(_Hint(ev.log_index, venue, token, token_delta, quote_delta, asset, user))
    return out


def _venue_token_amounts(bundle: TxBundle, venue: str, token: str) -> set[int]:
    return {
        leg.amount
        for leg in bundle.transfers
        if leg.token == token and leg.amount > 0 and venue in (leg.from_addr, leg.to_addr)
    }


def _hint_quote_asset(
    bundle: TxBundle, venue: str, quote: int, quote_assets: frozenset[str], singleton: bool
) -> str | None:
    """Which asset the venue's reported quote was paid in, or None when nothing moved to back it.

    A pair reports both sides of its swap as ERC-20 movements, so a quote amount that matches no transfer at
    that venue is not a price anyone paid. Taking it anyway is how an amount from a pool event became 523
    trillion MON of cost basis. A singleton pool manager is the exception: it can settle against its own
    claim balances, so its event really is the only record of the value that moved.
    """
    matching: dict[str, int] = defaultdict(int)
    for leg in bundle.transfers:
        if leg.token not in quote_assets or venue not in (leg.from_addr, leg.to_addr):
            continue
        if _within_fee_tolerance(leg.amount, quote):
            matching[leg.token] += leg.amount
    if matching:
        return max(matching.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if singleton:
        return NATIVE
    return None


def _own_quote(raw: dict[str, int], rates: Rates) -> tuple[str, int] | None:
    mon_parts = {a: _to_native_units(a, v, rates) for a, v in raw.items() if a in MON_FAMILY and v}
    usd_parts = {a: v for a, v in raw.items() if a not in MON_FAMILY and v}
    mon = sum(mon_parts.values())
    usd = sum(usd_parts.values())
    if mon == 0 and usd == 0:
        return None
    use_mon = usd == 0
    if mon != 0 and usd != 0:
        usd_as_mon = Decimal(0)
        if rates.usdc_per_mon > 0:
            usd_as_mon = Decimal(abs(usd)) / USD_UNIT / rates.usdc_per_mon * WEI
        use_mon = Decimal(abs(mon)) >= usd_as_mon
    if use_mon:
        asset = next(iter(mon_parts)) if len(mon_parts) == 1 else NATIVE
        return asset, mon
    asset = next(iter(usd_parts)) if len(usd_parts) == 1 else USDC
    return asset, usd


def _values(asset: str | None, quote_delta: int | None, rates: Rates) -> tuple[Decimal, Decimal]:
    if asset is None or not quote_delta:
        return Decimal(0), Decimal(0)
    if asset in MON_FAMILY:
        mon = Decimal(abs(quote_delta)) / WEI
        return mon, mon * rates.mon_usd
    usd = Decimal(abs(quote_delta)) / USD_UNIT
    mon = usd / rates.usdc_per_mon if rates.usdc_per_mon > 0 else Decimal(0)
    return mon, usd


def _price(mon_value: Decimal, token_delta: int) -> Decimal | None:
    if mon_value <= 0 or token_delta == 0:
        return None
    return mon_value * WEI / Decimal(abs(token_delta))


def _hint_price(hints: list[_Hint], rates: Rates) -> Decimal | None:
    tokens = sum(abs(h.token_delta) for h in hints)
    if tokens == 0:
        return None
    mon = Decimal(0)
    for h in hints:
        m, _ = _values(h.quote_asset, _to_native_units(h.quote_asset, h.quote_delta, rates), rates)
        mon += m
    return _price(mon, tokens)


def _is_mon(asset: str) -> bool:
    return asset in MON_FAMILY


def _same_family(hints: list[_Hint]) -> list[_Hint]:
    mon = _is_mon(hints[0].quote_asset)
    seen: dict[int, _Hint] = {}
    for h in hints:
        if _is_mon(h.quote_asset) == mon and h.log_index not in seen:
            seen[h.log_index] = h
    return list(seen.values())


def _sum_hints(hints: list[_Hint], rates: Rates) -> tuple[str, int]:
    assets = {h.quote_asset for h in hints}
    if len(assets) == 1:
        asset = next(iter(assets))
    else:
        asset = NATIVE if _is_mon(hints[0].quote_asset) else USDC
    return asset, sum(_to_native_units(h.quote_asset, h.quote_delta, rates) for h in hints)


def _assign_hints(leg: _Leg, hints: list[_Hint], rates: Rates, basis_state: str, single: bool) -> None:
    asset, quote = _sum_hints(hints, rates)
    if quote == 0 or _same_sign(quote, leg.token_delta):
        return
    leg.quote_asset = asset
    leg.quote_delta = quote
    leg.source = SOURCE_VENUE_EVENT
    leg.basis_state = basis_state
    leg.hint_price = _hint_price(hints, rates)
    if single and leg.venue is None:
        leg.venue = hints[0].venue


def _assign_scaled(leg: _Leg, hints: list[_Hint], rates: Rates, single: bool) -> None:
    asset, quote = _sum_hints(hints, rates)
    hint_tokens = sum(abs(h.token_delta) for h in hints)
    if quote == 0 or hint_tokens == 0 or _same_sign(quote, leg.token_delta):
        return
    scaled = abs(quote) * abs(leg.token_delta) // hint_tokens
    leg.quote_asset = asset
    leg.quote_delta = scaled if quote > 0 else -scaled
    leg.source = SOURCE_VENUE_EVENT
    leg.basis_state = BASIS_ESTIMATED
    leg.hint_price = _hint_price(hints, rates)
    if single and leg.venue is None:
        leg.venue = hints[0].venue


def _closest_hint(leg: _Leg, candidates: list[_Hint]) -> _Hint | None:
    if not candidates:
        return None
    return min(candidates, key=lambda h: (abs(h.token_delta - leg.token_delta), h.log_index))


def _exact_hints(leg: _Leg, hints: list[_Hint], used: set[int]) -> list[_Hint]:
    same_side = [
        h
        for h in hints
        if h.log_index not in used and h.token == leg.token and _same_sign(h.token_delta, leg.token_delta)
    ]
    exact = [h for h in same_side if abs(h.token_delta - leg.token_delta) <= AMOUNT_TOLERANCE_WEI]
    if exact:
        return [min(exact, key=lambda h: h.log_index)]
    if same_side and abs(sum(h.token_delta for h in same_side) - leg.token_delta) <= AMOUNT_TOLERANCE_WEI:
        return sorted(same_side, key=lambda h: h.log_index)
    return []


def _prefer_venue_quote(leg: _Leg, matched: list[_Hint], own_asset: str, own_delta: int, rates: Rates) -> None:
    asset, quote = _sum_hints(matched, rates)
    if quote == 0 or _same_sign(quote, leg.token_delta) or _is_mon(asset) != _is_mon(own_asset):
        return
    if Decimal(abs(quote - own_delta)) > Decimal(abs(quote)) * FEE_TOLERANCE:
        return
    leg.hint_price = _hint_price(matched, rates)
    if leg.venue is None:
        leg.venue = matched[0].venue


def _within_fee_tolerance(leg_delta: int, hint_delta: int) -> bool:
    return abs(leg_delta - hint_delta) <= AMOUNT_TOLERANCE_WEI or (
        Decimal(abs(leg_delta - hint_delta)) <= Decimal(abs(hint_delta)) * FEE_TOLERANCE
    )


def _counterparty_and_venue(bundle: TxBundle, leg: _Leg, kinds: _Kinds) -> None:
    """Name the other side of this movement, then follow any routers behind it to the venue.

    The movement already knows its counterparty: it is the other end of the transfer that produced it. The
    largest neighbour of the whole transaction was the right answer only while a leg was a wallet's netted
    position; for a single movement it names whoever the wallet happened to trade the most with, so a 0.2%
    fee paid alongside a trade was recorded as having gone to the trading partner.
    """
    incoming = leg.token_delta > 0
    if leg.counterparty is None:
        leg.counterparty = _dominant_neighbor(bundle, leg.token, leg.wallet, incoming, exclude=set())
    direct = leg.counterparty
    if direct is None:
        return
    node = direct
    visited = {leg.wallet, direct}
    hops = 0
    while kinds.of(node) == KIND_VENUE_ROUTER and hops < MAX_ROUTER_HOPS:
        nxt = _dominant_neighbor(bundle, leg.token, node, incoming, exclude=visited)
        if nxt is None:
            break
        visited.add(nxt)
        node = nxt
        hops += 1
    if kinds.of(node) in VENUE_KINDS:
        leg.venue = node


def _dominant_neighbor(bundle: TxBundle, token: str, addr: str, incoming: bool, exclude: set[str]) -> str | None:
    totals: dict[str, int] = defaultdict(int)
    for t in bundle.transfers:
        if t.token != token or t.amount <= 0:
            continue
        if incoming and t.to_addr == addr and t.from_addr not in exclude:
            totals[t.from_addr] += t.amount
        elif not incoming and t.from_addr == addr and t.to_addr not in exclude:
            totals[t.to_addr] += t.amount
    if not totals:
        return None
    return max(totals.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _shares_moved(bundle: TxBundle, wallet: str, issuer: str, incoming_tokens: bool) -> bool:
    for t in bundle.transfers:
        if t.token != issuer or t.amount <= 0:
            continue
        if incoming_tokens and t.from_addr == wallet:
            return True
        if not incoming_tokens and t.to_addr == wallet:
            return True
    return False


def _classify_unpriced(
    leg: _Leg, bundle: TxBundle, kinds: _Kinds, origin: str | None, reference_price: PriceFn | None
) -> None:
    incoming = leg.token_delta > 0
    cp = leg.counterparty
    ck = kinds.of(cp) if cp else ""
    leg.quote_asset = None
    leg.quote_delta = None
    leg.source = SOURCE_TRANSFER_NET
    if cp is None or ck == KIND_ZERO:
        if incoming:
            leg.kind = KIND_MINT if origin == leg.wallet else KIND_AIRDROP
            leg.basis_state = BASIS_UNRESOLVED
        else:
            leg.kind = KIND_BURN
            leg.basis_state = BASIS_OBSERVED
        return
    if ck not in WALLET_KINDS and _shares_moved(bundle, leg.wallet, cp, incoming):
        if ck == KIND_VENUE_POOL:
            leg.kind = KIND_LP_REMOVE if incoming else KIND_LP_ADD
        else:
            leg.kind = KIND_VAULT_WITHDRAW if incoming else KIND_VAULT_DEPOSIT
        leg.basis_state = BASIS_OBSERVED
        return
    if ck == KIND_VENUE_CUSTODY:
        leg.kind = KIND_CUSTODY_WITHDRAW if incoming else KIND_CUSTODY_DEPOSIT
        leg.basis_state = BASIS_OBSERVED
        return
    if leg.venue is not None or ck in VENUE_KINDS:
        leg.kind = KIND_BUY if incoming else KIND_SELL
        _price_by_reference(leg, reference_price)
        return
    leg.kind = KIND_TRANSFER_IN if incoming else KIND_TRANSFER_OUT
    leg.basis_state = BASIS_UNRESOLVED if incoming else BASIS_OBSERVED


def _price_by_reference(leg: _Leg, reference_price: PriceFn | None) -> None:
    price = reference_price(leg.token) if reference_price else None
    if price is None or price <= 0:
        leg.basis_state = BASIS_UNRESOLVED
        return
    quote = -int(Decimal(leg.token_delta) * price)
    if quote == 0:
        leg.basis_state = BASIS_UNRESOLVED
        return
    leg.quote_asset = NATIVE
    leg.quote_delta = quote
    leg.source = SOURCE_RECONCILE
    leg.basis_state = BASIS_ESTIMATED
    leg.hint_price = price


def _collect(bundle: TxBundle, tokens: set[str], quote_assets: frozenset[str]):
    token_deltas: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    first_log: dict[tuple[str, str], int] = {}
    quote_raw: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    quote_sources: dict[str, set[str]] = defaultdict(set)
    for leg in bundle.transfers:
        if leg.amount <= 0:
            continue
        if leg.token in tokens:
            for addr in (leg.from_addr, leg.to_addr):
                key = (addr, leg.token)
                first_log[key] = min(first_log.get(key, leg.log_index), leg.log_index)
            token_deltas[leg.token][leg.from_addr] -= leg.amount
            token_deltas[leg.token][leg.to_addr] += leg.amount
        elif leg.token in quote_assets:
            quote_raw[leg.from_addr][leg.token] -= leg.amount
            quote_raw[leg.to_addr][leg.token] += leg.amount
            quote_sources[leg.from_addr].add(SOURCE_TRANSFER_NET)
            quote_sources[leg.to_addr].add(SOURCE_TRANSFER_NET)
    meta = bundle.meta
    top = None
    if meta and meta.value and meta.value > 0:
        top = ((meta.from_addr or "").lower(), (meta.to_addr or "").lower(), int(meta.value))
        if top[0]:
            quote_raw[top[0]][NATIVE] -= top[2]
            quote_sources[top[0]].add(SOURCE_TRANSFER_NET)
        if top[1]:
            quote_raw[top[1]][NATIVE] += top[2]
            quote_sources[top[1]].add(SOURCE_TRANSFER_NET)
    trace = bundle.trace
    if trace and trace.available:
        skipped_top = False
        for src, dst, value in trace.transfers:
            src, dst, value = (src or "").lower(), (dst or "").lower(), int(value or 0)
            if value <= 0:
                continue
            if not skipped_top and top is not None and (src, dst, value) == top:
                skipped_top = True
                continue
            quote_raw[src][NATIVE] -= value
            quote_raw[dst][NATIVE] += value
            quote_sources[src].add(SOURCE_TRACE)
            quote_sources[dst].add(SOURCE_TRACE)
    return token_deltas, first_log, quote_raw, quote_sources


@dataclass
class _Move:
    log_index: int
    wallet: str
    token: str
    delta: int
    counterparty: str


@dataclass
class _QuoteMove:
    log_index: int
    wallet: str
    asset: str
    delta: int
    counterparty: str
    source: str = SOURCE_TRANSFER_NET


def _movements(bundle: TxBundle, tokens: set[str], quote_assets: frozenset[str]):
    """Every observed movement kept apart; netting them per wallet is what erases actions."""
    moves: list[_Move] = []
    quotes: list[_QuoteMove] = []
    for leg in bundle.transfers:
        if leg.amount <= 0:
            continue
        if leg.token in tokens:
            moves.append(_Move(leg.log_index, leg.from_addr, leg.token, -leg.amount, leg.to_addr))
            moves.append(_Move(leg.log_index, leg.to_addr, leg.token, leg.amount, leg.from_addr))
        elif leg.token in quote_assets:
            quotes.append(_QuoteMove(leg.log_index, leg.from_addr, leg.token, -leg.amount, leg.to_addr))
            quotes.append(_QuoteMove(leg.log_index, leg.to_addr, leg.token, leg.amount, leg.from_addr))
    meta = bundle.meta
    top = None
    if meta and meta.value and meta.value > 0:
        src, dst = (meta.from_addr or "").lower(), (meta.to_addr or "").lower()
        top = (src, dst, int(meta.value))
        if src:
            quotes.append(_QuoteMove(-1, src, NATIVE, -int(meta.value), dst))
        if dst:
            quotes.append(_QuoteMove(-1, dst, NATIVE, int(meta.value), src))
    trace = bundle.trace
    if trace and trace.available:
        skipped_top = False
        for src, dst, value in trace.transfers:
            src, dst, value = (src or "").lower(), (dst or "").lower(), int(value or 0)
            if value <= 0:
                continue
            if not skipped_top and top is not None and (src, dst, value) == top:
                skipped_top = True
                continue
            quotes.append(_QuoteMove(-1, src, NATIVE, -value, dst, SOURCE_TRACE))
            quotes.append(_QuoteMove(-1, dst, NATIVE, value, src, SOURCE_TRACE))
    return moves, quotes


def _venue_movements(
    bundle: TxBundle,
    tokens: set[str],
    pools: dict[str, tuple[str, str, bool]] | None,
    kinds: _Kinds,
) -> tuple[list[_Move], list[_QuoteMove]]:
    """Movements a swap event reports when no ERC-20 carried them.

    Uniswap V4 can settle a swap against the pool manager's internal claim balances, so a real trade can
    leave no transfer at all. Those transactions are invisible to any rule that starts from transfers, and
    on moncock alone 26 wallet-transactions vanish that way. The pool's own registration says which token
    it trades, so the event is sufficient evidence on its own. The actor comes from the event, never from
    the transaction origin, which is a bundler or a relayer as often as it is the trader.
    """
    t_moves: list[_Move] = []
    q_moves: list[_QuoteMove] = []
    if not pools:
        return t_moves, q_moves
    for ev in bundle.venue_events:
        if ev.tag not in SETTLING_TAGS:
            continue
        parsed = ev.parsed or {}
        venue = (ev.address or "").lower()
        if not _trusted_emitter(venue, kinds):
            continue
        key = str(parsed.get("pool_id") or venue).lower()
        info = pools.get(key)
        if not info:
            continue
        token, quote, token_is_0 = info
        token = (token or "").lower()
        if token not in tokens or _venue_token_amounts(bundle, venue, token):
            continue
        sign = 1 if ev.tag == "V4SWAP" else -1
        a0 = sign * int(parsed.get("amount0") or 0)
        a1 = sign * int(parsed.get("amount1") or 0)
        if a0 == 0 or a1 == 0 or _same_sign(a0, a1):
            continue
        token_delta, quote_delta = (a0, a1) if token_is_0 else (a1, a0)
        actor = (parsed.get("user") or parsed.get("sender") or "").lower()
        if not actor or not kinds.is_wallet(actor):
            continue
        t_moves.append(_Move(ev.log_index, actor, token, token_delta, venue))
        q_moves.append(_QuoteMove(ev.log_index, actor, (quote or NATIVE).lower(), quote_delta, venue))
    return t_moves, q_moves


def _quote_total(assigned: list[_QuoteMove], rates: Rates) -> tuple[str, int, str]:
    """Conserve every quote leg; mixed currencies collapse to their MON equivalent, never to one family."""
    assets = {q.asset for q in assigned}
    source = SOURCE_TRACE if {q.source for q in assigned} == {SOURCE_TRACE} else SOURCE_TRANSFER_NET
    if len(assets) == 1:
        asset = next(iter(assets))
        return asset, _to_native_units(asset, sum(q.delta for q in assigned), rates), source
    total = Decimal(0)
    for q in assigned:
        if q.asset in MON_FAMILY:
            total += Decimal(_to_native_units(q.asset, q.delta, rates))
        elif rates.usdc_per_mon > 0:
            total += Decimal(q.delta) / USD_UNIT / rates.usdc_per_mon * WEI
    return NATIVE, int(total), source


def _less_conversions(assigned: list[_QuoteMove], spare: list[_QuoteMove]) -> list[_QuoteMove]:
    """Cancel a wrap against the payment it funded.

    Receiving WMON from the zero address and then spending it is one payment, not income plus a payment,
    so an unmatched inbound leg offsets a matched outbound leg of the same asset. A sale's proceeds are
    never spare, because they were matched to the disposal that earned them.
    """
    if not spare or not assigned:
        return assigned
    out: list[_QuoteMove] = []
    for q in assigned:
        remaining = q.delta
        for other in spare:
            if other.asset != q.asset or _same_sign(other.delta, remaining) or not remaining:
                continue
            take = min(abs(remaining), abs(other.delta))
            remaining += take if remaining < 0 else -take
            other.delta += take if other.delta < 0 else -take
        if remaining:
            out.append(_QuoteMove(q.log_index, q.wallet, q.asset, remaining, q.counterparty, q.source))
    return out


def _payment_reaches(quote: _QuoteMove, party: str, quotes: list[_QuoteMove]) -> bool:
    """True when a payment moved between the wallet and party, directly or through one intermediary.

    An escrow or settlement contract standing between two counterparties is still one payment, so a single
    hop is followed. What arrives has to be what was sent: a payment forwarded through an escrow keeps its
    amount, and requiring that is what separates it from an unrelated refund that merely happens to pass
    through the same contract. Without it, a 99 USDC refund elsewhere in the transaction could pay for a
    hand-off of 165,765 MON worth of tokens, and the wallet that handed them over booked the difference as a
    loss it never took.
    """
    if quote.counterparty == party:
        return True
    for other in quotes:
        if other.wallet != quote.counterparty:
            continue
        if other.asset != quote.asset or other.counterparty != party:
            continue
        if _within_fee_tolerance(abs(other.delta), abs(quote.delta)):
            return True
    return False


def _match_actions(moves: list[_Move], quotes: list[_QuoteMove], kinds: _Kinds, rates: Rates):
    """Pair each token movement with the payment that funded it.

    A payment counts only when it reached the party the tokens came from. Where that party is a venue or a
    router, any non-wallet counterparty qualifies, because a routed trade pays a different hop than it
    receives from. Where it is an ordinary wallet the match must be exact, which is what stops an unrelated
    payment elsewhere in the same transaction from reading as a purchase.
    """
    actions: list[tuple[_Move, list[_QuoteMove]]] = []
    by_wallet: dict[str, list[_Move]] = defaultdict(list)
    for m in moves:
        if m.delta and kinds.is_wallet(m.wallet):
            by_wallet[m.wallet].append(m)
    quotes_by_wallet: dict[str, list[_QuoteMove]] = defaultdict(list)
    for q in quotes:
        if q.delta:
            quotes_by_wallet[q.wallet].append(q)

    for wallet in sorted(by_wallet):
        wallet_moves = sorted(by_wallet[wallet], key=lambda m: (m.log_index, m.token))
        assigned: dict[int, list[_QuoteMove]] = {i: [] for i in range(len(wallet_moves))}
        spare: list[_QuoteMove] = []
        for q in sorted(quotes_by_wallet.get(wallet, []), key=lambda q: q.log_index):
            best = None
            best_gap = None
            for i, m in enumerate(wallet_moves):
                if _same_sign(q.delta, m.delta):
                    continue
                if kinds.is_wallet(m.counterparty) and not _payment_reaches(q, m.counterparty, quotes):
                    continue
                if not kinds.is_wallet(m.counterparty) and kinds.is_wallet(q.counterparty):
                    continue
                gap = abs(q.log_index - m.log_index)
                if best_gap is None or gap < best_gap:
                    best = i
                    best_gap = gap
            if best is not None:
                assigned[best].append(q)
            else:
                spare.append(q)
        for i, m in enumerate(wallet_moves):
            actions.append((m, _less_conversions(assigned[i], spare)))
    return actions


def _resolve_wallet(
    legs: list[_Leg],
    own: tuple[str, int] | None,
    own_source: str,
    hints: list[_Hint],
    used: set[int],
    rates: Rates,
) -> None:
    event_quotes: dict[str, _Hint] = {}
    for leg in legs:
        open_hints = [
            h
            for h in hints
            if h.log_index not in used and h.token == leg.token and _same_sign(h.token_delta, leg.token_delta)
        ]
        hint = _closest_hint(leg, [h for h in open_hints if h.user == leg.wallet])
        if hint is None:
            exact = [h for h in open_hints if abs(h.token_delta - leg.token_delta) <= AMOUNT_TOLERANCE_WEI]
            hint = _closest_hint(leg, exact)
        if hint is not None:
            used.add(hint.log_index)
            event_quotes[leg.token] = hint
    own_asset, own_delta = own if own else (None, 0)
    if len(legs) == 1:
        leg = legs[0]
        if own_delta and not _same_sign(own_delta, leg.token_delta):
            leg.quote_asset, leg.quote_delta = own_asset, own_delta
            leg.source = own_source
            leg.basis_state = BASIS_OBSERVED
            hint = event_quotes.get(leg.token)
            if hint is None:
                matched = _exact_hints(leg, hints, used)
            elif abs(hint.token_delta - leg.token_delta) <= AMOUNT_TOLERANCE_WEI:
                matched = [hint]
            else:
                matched = []
            if matched:
                for h in matched:
                    used.add(h.log_index)
                _prefer_venue_quote(leg, matched, own_asset, own_delta, rates)
                return
            if hint is None:
                near = [
                    h
                    for h in hints
                    if h.log_index not in used
                    and h.token == leg.token
                    and _same_sign(h.token_delta, leg.token_delta)
                    and _within_fee_tolerance(leg.token_delta, h.token_delta)
                ]
                hint = _closest_hint(leg, near)
                if hint is not None:
                    used.add(hint.log_index)
            if hint is not None:
                leg.hint_price = _hint_price([hint], rates)
                if leg.venue is None:
                    leg.venue = hint.venue
        elif leg.token in event_quotes:
            _assign_hints(leg, [event_quotes[leg.token]], rates, BASIS_OBSERVED, single=True)
        return
    spent = 0
    for leg in legs:
        hint = event_quotes.get(leg.token)
        if hint is not None:
            _assign_hints(leg, [hint], rates, BASIS_OBSERVED, single=True)
            if leg.resolved and own_asset is not None and leg.quote_asset == own_asset:
                spent += leg.quote_delta or 0
    remaining = [leg for leg in legs if not leg.resolved]
    if own_delta and len(remaining) == 1:
        residual = own_delta - spent
        leg = remaining[0]
        if residual and not _same_sign(residual, leg.token_delta):
            leg.quote_asset, leg.quote_delta = own_asset, residual
            leg.source = own_source
            leg.basis_state = BASIS_OBSERVED
        return
    if (
        not own_delta
        and len(legs) == 2
        and len(remaining) == 1
        and not _same_sign(legs[0].token_delta, legs[1].token_delta)
    ):
        done = next(leg for leg in legs if leg.resolved)
        leg = remaining[0]
        if done.quote_delta and not _same_sign(-done.quote_delta, leg.token_delta):
            leg.quote_asset = done.quote_asset
            leg.quote_delta = -done.quote_delta
            leg.source = SOURCE_VENUE_EVENT
            leg.basis_state = BASIS_ESTIMATED
            leg.hint_price = None


def _resolve_across_wallets(legs: list[_Leg], hints: list[_Hint], used: set[int], rates: Rates) -> None:
    groups: dict[tuple[str, bool], list[_Leg]] = defaultdict(list)
    for leg in legs:
        if not leg.resolved:
            groups[(leg.token, leg.token_delta > 0)].append(leg)
    for (token, incoming), members in sorted(groups.items()):
        open_hints = [
            h for h in hints if h.log_index not in used and h.token == token and (h.token_delta > 0) == incoming
        ]
        if not open_hints:
            continue
        picked = _same_family(open_hints)
        for h in picked:
            used.add(h.log_index)
        members.sort(key=lambda leg: leg.wallet)
        if len(members) == 1:
            leg = members[0]
            hint_tokens = sum(abs(h.token_delta) for h in picked)
            covered = (
                _within_fee_tolerance(abs(leg.token_delta), hint_tokens)
                and abs(leg.token_delta) <= hint_tokens + AMOUNT_TOLERANCE_WEI
            )
            if covered:
                _assign_hints(leg, picked, rates, BASIS_OBSERVED, single=len(picked) == 1)
            else:
                _assign_scaled(leg, picked, rates, single=len(picked) == 1)
            continue
        asset, total_quote = _sum_hints(picked, rates)
        total_tokens = sum(abs(leg.token_delta) for leg in members)
        if total_quote == 0 or total_tokens == 0 or _same_sign(total_quote, members[0].token_delta):
            continue
        price = _hint_price(picked, rates)
        allocated = 0
        for i, leg in enumerate(members):
            if i == len(members) - 1:
                share = total_quote - allocated
            else:
                share = total_quote * abs(leg.token_delta) // total_tokens
            allocated += share
            if share == 0:
                continue
            leg.quote_asset = asset
            leg.quote_delta = share
            leg.source = SOURCE_VENUE_EVENT
            leg.basis_state = BASIS_ESTIMATED
            leg.hint_price = price
            if leg.venue is None and len(picked) == 1:
                leg.venue = picked[0].venue


def _swap_pairs(legs_by_wallet: dict[str, list[_Leg]], reference_price: PriceFn | None) -> None:
    """Two unpriced movements in opposite directions are a swap only if they are two different tokens.

    One token leaving and returning is a wallet sending to itself, which changes nothing. Priced as a swap
    it becomes a sale and a purchase that never happened, and since the two cancel, no quantity check can
    see it.
    """
    for legs in legs_by_wallet.values():
        pending = [leg for leg in legs if not leg.resolved and leg.kind is None]
        if len(pending) != 2 or _same_sign(pending[0].token_delta, pending[1].token_delta):
            continue
        if pending[0].token == pending[1].token:
            continue
        for leg in pending:
            leg.kind = KIND_SWAP_LEG
            _price_by_reference(leg, reference_price)


def _sub_indices(legs: list[_Leg]) -> dict[int, int]:
    """Separate the movements that share a chain position, outgoing first.

    A transfer is one log and two movements, so the log index alone cannot identify either of them: keyed on
    it alone the sender and the receiver collide on the primary key and one is silently dropped on insert.
    Ordering the outgoing side first is not cosmetic either: it is what lets a fold in chain order release a
    sender's basis before the receiver inherits it, at the one position where both happen at once.
    """
    out: dict[int, int] = {}
    by_position: dict[int, list[_Leg]] = defaultdict(list)
    for leg in legs:
        by_position[leg.log_index].append(leg)
    for position in by_position.values():
        position.sort(key=lambda leg: (leg.token_delta > 0, leg.wallet, leg.token))
        for ordinal, leg in enumerate(position):
            out[id(leg)] = ordinal
    return out


def _flow(bundle: TxBundle, leg: _Leg, sub_index: int, origin: str | None, rates: Rates) -> Flow:
    mon_value, usd_value = _values(leg.quote_asset, leg.quote_delta, rates)
    price = _price(mon_value, leg.token_delta) if leg.basis_state == BASIS_OBSERVED else None
    if price is None:
        price = leg.hint_price
    return Flow(
        block_number=bundle.block_number,
        tx_index=bundle.tx_index,
        log_index=leg.log_index,
        sub_index=sub_index,
        txhash=bundle.txhash,
        timestamp=bundle.timestamp,
        wallet=leg.wallet,
        token=leg.token,
        token_delta=leg.token_delta,
        quote_asset=leg.quote_asset,
        quote_delta=leg.quote_delta,
        mon_value=mon_value,
        usd_value=usd_value,
        kind=leg.kind or KIND_TRANSFER_IN,
        venue=leg.venue,
        counterparty=leg.counterparty,
        origin=origin,
        source=leg.source,
        basis_state=leg.basis_state,
        price_native=price,
    )


def _sold_by(moves: list[_Move]) -> dict[str, dict[str, int]]:
    """How much of each token each wallet actually sent, which is not the same as its net position."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for move in moves:
        if move.delta < 0:
            out[move.token][move.wallet] += -move.delta
    return out


def _seller_hints(
    moves: list[_Move],
    quote_raw: dict[str, dict[str, int]],
    tx_tokens: set[str],
    kinds: _Kinds,
    rates: Rates,
) -> list[_Hint]:
    """What a wallet with no venue event of its own implies about the price, from what it was paid.

    The quantity has to be what the wallet sent, not what it was left holding. A relayer that
    forwards everything it receives nets to dust, and pairing that dust with the whole payment it
    collected implies a price twelve orders of magnitude too high, which then prices the movement
    that really passed through it.
    """
    sold = _sold_by(moves)
    out: list[_Hint] = []
    for token in sorted(tx_tokens):
        for addr, quantity in sorted(sold.get(token, {}).items()):
            if quantity <= 0 or not kinds.is_wallet(addr):
                continue
            own = _own_quote(quote_raw.get(addr, {}), rates)
            if own is None or own[1] <= 0:
                continue
            out.append(_Hint(-(len(out) + 1), addr, token, quantity, -own[1], own[0], None))
    return out


def net_transaction(
    bundle: TxBundle,
    registry: dict[str, TokenReg],
    kind_of: KindFn,
    quote_assets: frozenset[str] | set[str] = QUOTE_ASSETS,
    rates: Rates | None = None,
    reference_price: PriceFn | None = None,
    markets: dict[str, tuple[str, str]] | None = None,
    pools: dict[str, tuple[str, str, bool]] | None = None,
) -> list[Flow]:
    rates = rates or Rates()
    quote_assets = frozenset(a.lower() for a in quote_assets)
    tokens = {t.lower() for t, reg in registry.items() if reg.active and t.lower() not in quote_assets}
    kinds = _Kinds(kind_of)
    token_deltas, first_log, quote_raw, quote_sources = _collect(bundle, tokens, quote_assets)
    origin = (bundle.userop_sender or (bundle.meta.from_addr if bundle.meta else None) or "").lower() or None

    moves, quote_moves = _movements(bundle, tokens, quote_assets)
    extra_moves, extra_quotes = _venue_movements(bundle, tokens, pools, kinds)
    moves += extra_moves
    quote_moves += extra_quotes
    actions = _match_actions(moves, quote_moves, kinds, rates)
    legs_by_wallet: dict[str, list[_Leg]] = defaultdict(list)
    own_by_leg: dict[int, tuple[tuple[str, int] | None, str]] = {}
    for move, assigned in actions:
        leg = _Leg(move.wallet, move.token, move.delta, move.log_index, counterparty=move.counterparty or None)
        if assigned:
            asset, delta, source = _quote_total(assigned, rates)
            own_by_leg[id(leg)] = ((asset, delta), source)
        else:
            own_by_leg[id(leg)] = (None, SOURCE_TRANSFER_NET)
        legs_by_wallet[move.wallet].append(leg)
    if not legs_by_wallet:
        return []

    tx_tokens = {leg.token for legs in legs_by_wallet.values() for leg in legs}
    hints = _hints(bundle, tx_tokens, registry, quote_assets, kinds, markets)
    hints += _seller_hints(moves, quote_raw, tx_tokens, kinds, rates)
    used: set[int] = set()
    all_legs = [leg for legs in legs_by_wallet.values() for leg in legs]
    for leg in all_legs:
        _counterparty_and_venue(bundle, leg, kinds)

    for wallet in sorted(legs_by_wallet):
        for leg in legs_by_wallet[wallet]:
            own, own_source = own_by_leg[id(leg)]
            _resolve_wallet([leg], own, own_source, hints, used, rates)

    _resolve_across_wallets(all_legs, hints, used, rates)
    _swap_pairs(legs_by_wallet, reference_price)

    for leg in all_legs:
        if leg.kind is not None:
            continue
        if leg.resolved:
            leg.kind = KIND_BUY if leg.token_delta > 0 else KIND_SELL
        else:
            _classify_unpriced(leg, bundle, kinds, origin, reference_price)

    sub_index = _sub_indices(all_legs)
    all_legs.sort(key=lambda leg: (leg.wallet, leg.token, leg.log_index))
    return [_flow(bundle, leg, sub_index[id(leg)], origin, rates) for leg in all_legs]
