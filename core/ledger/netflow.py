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


def _hints(
    bundle: TxBundle,
    tx_tokens: set[str],
    registry: dict[str, TokenReg],
    quote_assets: frozenset[str],
    markets: dict[str, tuple[str, str]] | None = None,
) -> list[_Hint]:
    out: list[_Hint] = []
    for ev in bundle.venue_events:
        parsed = ev.parsed or {}
        venue = (ev.address or "").lower()
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
            asset = _venue_quote_asset(bundle, venue, quote_assets)
            for token in sorted(tx_tokens):
                for token_delta, quote_delta in ((w0, w1), (w1, w0)):
                    out.append(_Hint(ev.log_index, venue, token, token_delta, quote_delta, asset, user))
    return out


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
    if quote != own_delta:
        leg.quote_delta = quote
        leg.source = SOURCE_VENUE_EVENT
    leg.hint_price = _hint_price(matched, rates)
    if leg.venue is None:
        leg.venue = matched[0].venue


def _within_fee_tolerance(leg_delta: int, hint_delta: int) -> bool:
    return abs(leg_delta - hint_delta) <= AMOUNT_TOLERANCE_WEI or (
        Decimal(abs(leg_delta - hint_delta)) <= Decimal(abs(hint_delta)) * FEE_TOLERANCE
    )


def _counterparty_and_venue(bundle: TxBundle, leg: _Leg, kinds: _Kinds) -> None:
    incoming = leg.token_delta > 0
    direct = _dominant_neighbor(bundle, leg.token, leg.wallet, incoming, exclude=set())
    if direct is None:
        return
    leg.counterparty = direct
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
    leg.quote_asset = NATIVE
    leg.quote_delta = -int(Decimal(leg.token_delta) * price)
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
            basis = BASIS_OBSERVED if covered else BASIS_ESTIMATED
            _assign_hints(leg, picked, rates, basis, single=len(picked) == 1)
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
    for legs in legs_by_wallet.values():
        pending = [leg for leg in legs if not leg.resolved and leg.kind is None]
        if len(pending) != 2 or _same_sign(pending[0].token_delta, pending[1].token_delta):
            continue
        for leg in pending:
            leg.kind = KIND_SWAP_LEG
            _price_by_reference(leg, reference_price)


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


def net_transaction(
    bundle: TxBundle,
    registry: dict[str, TokenReg],
    kind_of: KindFn,
    quote_assets: frozenset[str] | set[str] = QUOTE_ASSETS,
    rates: Rates | None = None,
    reference_price: PriceFn | None = None,
    markets: dict[str, tuple[str, str]] | None = None,
) -> list[Flow]:
    rates = rates or Rates()
    quote_assets = frozenset(a.lower() for a in quote_assets)
    tokens = {t.lower() for t, reg in registry.items() if reg.active and t.lower() not in quote_assets}
    kinds = _Kinds(kind_of)
    token_deltas, first_log, quote_raw, quote_sources = _collect(bundle, tokens, quote_assets)
    origin = (bundle.userop_sender or (bundle.meta.from_addr if bundle.meta else None) or "").lower() or None

    legs_by_wallet: dict[str, list[_Leg]] = defaultdict(list)
    for token in sorted(token_deltas):
        for addr, delta in sorted(token_deltas[token].items()):
            if delta == 0 or not kinds.is_wallet(addr):
                continue
            legs_by_wallet[addr].append(_Leg(addr, token, delta, first_log[(addr, token)]))
    if not legs_by_wallet:
        return []

    tx_tokens = {leg.token for legs in legs_by_wallet.values() for leg in legs}
    hints = _hints(bundle, tx_tokens, registry, quote_assets, markets)
    used: set[int] = set()
    all_legs = [leg for legs in legs_by_wallet.values() for leg in legs]
    for leg in all_legs:
        _counterparty_and_venue(bundle, leg, kinds)

    for wallet in sorted(legs_by_wallet):
        own = _own_quote(quote_raw.get(wallet, {}), rates)
        sources = quote_sources.get(wallet, set())
        own_source = SOURCE_TRACE if sources == {SOURCE_TRACE} else SOURCE_TRANSFER_NET
        _resolve_wallet(legs_by_wallet[wallet], own, own_source, hints, used, rates)

    _resolve_across_wallets(all_legs, hints, used, rates)
    _swap_pairs(legs_by_wallet, reference_price)

    for leg in all_legs:
        if leg.kind is not None:
            continue
        if leg.resolved:
            leg.kind = KIND_BUY if leg.token_delta > 0 else KIND_SELL
        else:
            _classify_unpriced(leg, bundle, kinds, origin, reference_price)

    all_legs.sort(key=lambda leg: (leg.wallet, leg.token))
    return [_flow(bundle, leg, i, origin, rates) for i, leg in enumerate(all_legs)]
