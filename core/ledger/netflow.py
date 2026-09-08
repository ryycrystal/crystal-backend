from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from core.ledger.types import (
    ACCOUNT_KINDS,
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
    KIND_TOKEN,
    KIND_TRANSFER_IN,
    KIND_TRANSFER_OUT,
    KIND_VAULT_DEPOSIT,
    KIND_VAULT_WITHDRAW,
    KIND_VENUE_CURVE,
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
PRICE_VENUE_KINDS = frozenset({KIND_VENUE_POOL, KIND_VENUE_CURVE})
AMOUNT_TOLERANCE_WEI = 1
FEE_TOLERANCE = Decimal("0.10")
MAX_PATH_HOPS = 6
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
        """May hold a position: a person's wallet, or a contract not known to be anything else."""
        return self.of(addr) in WALLET_KINDS

    def is_account(self, addr: str) -> bool:
        """A person's wallet: an EOA, a delegated EOA or a smart account. Never a venue, never a relay."""
        return self.of(addr) in ACCOUNT_KINDS


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


def _is_mon(asset: str) -> bool:
    return asset in MON_FAMILY


def _sum_hints(hints: list[_Hint], rates: Rates) -> tuple[str, int]:
    assets = {h.quote_asset for h in hints}
    if len(assets) == 1:
        asset = next(iter(assets))
    else:
        asset = NATIVE if _is_mon(hints[0].quote_asset) else USDC
    return asset, sum(_to_native_units(h.quote_asset, h.quote_delta, rates) for h in hints)


def _within_fee_tolerance(leg_delta: int, hint_delta: int) -> bool:
    return abs(leg_delta - hint_delta) <= AMOUNT_TOLERANCE_WEI or (
        Decimal(abs(leg_delta - hint_delta)) <= Decimal(abs(hint_delta)) * FEE_TOLERANCE
    )


def _shares_moved(bundle: TxBundle, wallet: str, issuer: str, incoming_tokens: bool) -> bool:
    for t in bundle.transfers:
        if t.token != issuer or t.amount <= 0:
            continue
        if incoming_tokens and t.from_addr == wallet:
            return True
        if not incoming_tokens and t.to_addr == wallet:
            return True
    return False


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
) -> tuple[list[_Move], list[_QuoteMove], list[_Hint]]:
    """Movements a swap event reports when no ERC-20 carried them.

    Uniswap V4 can settle a swap against the pool manager's internal claim balances, so a real trade can
    leave no transfer at all. Those transactions are invisible to any rule that starts from transfers, and
    on moncock alone 26 wallet-transactions vanish that way. The pool's own registration says which token
    it trades, so the event is sufficient evidence on its own. The actor comes from the event, never from
    the transaction origin, which is a bundler or a relayer as often as it is the trader. The event also
    stands as the venue's price, so a wallet the actor hands the tokens on to is priced from it.
    """
    t_moves: list[_Move] = []
    q_moves: list[_QuoteMove] = []
    hints: list[_Hint] = []
    if not pools:
        return t_moves, q_moves, hints
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
        if not actor or kinds.of(actor) in PRICE_VENUE_KINDS or kinds.of(actor) in (KIND_ZERO, KIND_TOKEN):
            continue
        asset = (quote or NATIVE).lower()
        t_moves.append(_Move(ev.log_index, actor, token, token_delta, venue))
        q_moves.append(_QuoteMove(ev.log_index, actor, asset, quote_delta, venue))
        hints.append(_Hint(ev.log_index, venue, token, token_delta, quote_delta, asset, actor))
    return t_moves, q_moves, hints


def _combine(parts: list[tuple[str, int]], rates: Rates) -> tuple[str, int]:
    """Conserve every quote leg; mixed currencies collapse to their MON equivalent, never to one family."""
    assets = {asset for asset, _ in parts}
    if len(assets) == 1:
        return next(iter(assets)), sum(delta for _, delta in parts)
    total = Decimal(0)
    for asset, delta in parts:
        if asset in MON_FAMILY:
            total += Decimal(delta)
        elif rates.usdc_per_mon > 0:
            total += Decimal(delta) / USD_UNIT / rates.usdc_per_mon * WEI
    return NATIVE, int(total)


def _quote_total(assigned: list[_QuoteMove], rates: Rates) -> tuple[str, int, str]:
    source = SOURCE_TRACE if {q.source for q in assigned} == {SOURCE_TRACE} else SOURCE_TRANSFER_NET
    asset, delta = _combine([(q.asset, _to_native_units(q.asset, q.delta, rates)) for q in assigned], rates)
    return asset, delta, source


def _less_conversions(assigned: list[_QuoteMove], spare: list[_QuoteMove], kinds: _Kinds) -> list[_QuoteMove]:
    """Cancel a wrap against the payment it funded.

    Receiving WMON from the zero address and then spending it is one payment, not income plus a payment,
    so an unmatched inbound leg offsets a matched outbound leg of the same asset. A sale's proceeds are
    never spare, because they were matched to the disposal that earned them. The same asset passing to or
    from a person's wallet is never a conversion: it is a payment, and cancelling it took the cost off a
    purchase that was paid on one hop and received from another.
    """
    if not spare or not assigned:
        return assigned
    out: list[_QuoteMove] = []
    for q in assigned:
        remaining = q.delta
        for other in spare:
            if other.asset != q.asset or _same_sign(other.delta, remaining) or not remaining:
                continue
            if kinds.is_account(other.counterparty):
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


class _Paths:
    """The token transfer graph of one transaction, with every contract that only passed tokens on made
    transparent.

    A router, an executor, a settlement contract or a relayer that ends the transaction holding exactly
    what it started with did not trade. Following a movement through such addresses to the venue or the
    wallet at the far end is what lets a purchase a solver paid for, or a sale whose proceeds went to a
    fourth address, be priced at what the venue was paid, and what lets a hand-off through a distributor
    name the wallet it really went to. A person's wallet is never transparent, whatever it did, and neither
    is a pool or a curve, because that is where the price was made.
    """

    def __init__(self, moves: list[_Move], kinds: _Kinds):
        self.inbound: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
        self.outbound: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
        self.received: dict[tuple[str, str], int] = defaultdict(int)
        self.sent: dict[tuple[str, str], int] = defaultdict(int)
        for m in moves:
            key = (m.token, m.wallet)
            if m.delta > 0:
                self.inbound[key].append((m.counterparty, m.delta))
                self.received[key] += m.delta
            elif m.delta < 0:
                self.outbound[key].append((m.counterparty, -m.delta))
                self.sent[key] += -m.delta
        self.transparent: set[tuple[str, str]] = set()
        for key in set(self.received) | set(self.sent):
            kind = kinds.of(key[1])
            if kind in ACCOUNT_KINDS or kind in PRICE_VENUE_KINDS or kind == KIND_ZERO:
                continue
            received, sent = self.received[key], self.sent[key]
            if received <= 0:
                continue
            kept = received - sent
            if abs(kept) <= AMOUNT_TOLERANCE_WEI:
                self.transparent.add(key)
            elif kind == KIND_VENUE_ROUTER and 0 < kept <= received * FEE_TOLERANCE:
                self.transparent.add(key)

    def far_ends(self, token: str, addr: str, amount: int, incoming: bool) -> list[tuple[str, int, str | None]]:
        """Where `amount` of the token that came from (or went to) `addr` really came from (or went to).

        Each entry is the party at the far end, the share of the amount that traces to it, and the
        transparent address adjacent to it, which is who paid or was paid on that leg.
        """
        return _merge_ends(self._walk(token, addr, amount, incoming, None, 0, frozenset()))

    def between(self, token: str, via: str, party: str, incoming: bool) -> int:
        legs = (self.inbound if incoming else self.outbound).get((token, via), [])
        return sum(amount for other, amount in legs if other == party)

    def _walk(
        self, token: str, addr: str, amount: int, incoming: bool, via: str | None, depth: int, seen: frozenset[str]
    ) -> list[tuple[str, int, str | None]]:
        key = (token, addr)
        if key not in self.transparent or depth >= MAX_PATH_HOPS or addr in seen:
            return [(addr, amount, via)]
        legs = (self.inbound if incoming else self.outbound).get(key, [])
        total = sum(a for _, a in legs)
        if total <= 0:
            return [(addr, amount, via)]
        own_side = self.sent[key] if incoming else self.received[key]
        carried = amount if abs(total - own_side) <= AMOUNT_TOLERANCE_WEI else total * amount // own_side
        out: list[tuple[str, int, str | None]] = []
        allocated = 0
        for i, (other, a) in enumerate(legs):
            share = carried - allocated if i == len(legs) - 1 else carried * a // total
            allocated += share
            if share > 0:
                out.extend(self._walk(token, other, share, incoming, addr, depth + 1, seen | {addr}))
        return out


def _merge_ends(ends: list[tuple[str, int, str | None]]) -> list[tuple[str, int, str | None]]:
    merged: dict[tuple[str, str | None], int] = {}
    for party, amount, via in ends:
        merged[(party, via)] = merged.get((party, via), 0) + amount
    return [(party, amount, via) for (party, via), amount in merged.items() if amount > 0]


def _match_actions(
    moves: list[_Move], quotes: list[_QuoteMove], kinds: _Kinds, paths: _Paths, rates: Rates
) -> list[tuple[_Move, list[_QuoteMove]]]:
    """Pair each token movement of a holder with the payment that funded it.

    A payment counts only when it reached the party the tokens came from. Where that party is a venue or
    a contract, any payment to a non-person qualifies, because a routed trade pays a different hop than it
    receives from. Where it is a person's wallet the match must be exact, which is what stops an unrelated
    payment elsewhere in the same transaction from reading as a purchase. Addresses that only passed the
    tokens on have no movements of their own to price.
    """
    actions: list[tuple[_Move, list[_QuoteMove]]] = []
    by_wallet: dict[str, list[_Move]] = defaultdict(list)
    for m in moves:
        if m.delta and kinds.is_wallet(m.wallet) and (m.token, m.wallet) not in paths.transparent:
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
                if kinds.is_account(m.counterparty) and not _payment_reaches(q, m.counterparty, quotes):
                    continue
                if not kinds.is_account(m.counterparty) and kinds.is_account(q.counterparty):
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
            actions.append((m, _less_conversions(assigned[i], spare, kinds)))
    return actions


def _venue_quote(
    bundle: TxBundle,
    paths: _Paths,
    venue: str,
    token: str,
    share: int,
    matched: int,
    incoming: bool,
    hints: list[_Hint],
    quotes_by_wallet: dict[str, list[_QuoteMove]],
    via: str | None,
    rates: Rates,
) -> tuple[str, int, bool] | None:
    """What the venue was paid for, or paid out on, `matched` of the token, scaled to the holder's `share`.

    The venue's own event is the record when there is one: a fill of exactly the holder's share is that
    holder's fill, a fill of exactly the amount that crossed the venue is shared by whoever received it, and
    otherwise the venue's fills are pooled. A fill recorded under a market rather than the contract that
    holds the tokens is matched by amount. Failing all of that, what the venue was paid in quote assets by
    the address that dealt with it, native MON settled by an internal call included, is scaled to the tokens
    it moved. That is exact for a single trade and an estimate otherwise, and is labelled as one: the v4
    pool manager's swaps before its topics were cached are only visible this way, through the trace.
    """
    side = [h for h in hints if h.token == token and (h.token_delta > 0) == incoming]
    same = [h for h in side if h.venue == venue]
    for pool, tokens in ((same, share), (same, matched)):
        exact = [h for h in pool if abs(abs(h.token_delta) - tokens) <= AMOUNT_TOLERANCE_WEI]
        if exact:
            h = min(exact, key=lambda h: h.log_index)
            quote = _to_native_units(h.quote_asset, h.quote_delta, rates)
            scaled = abs(quote) * share // tokens
            return h.quote_asset, (scaled if quote > 0 else -scaled), True
    if same:
        asset, total_quote = _sum_hints(same, rates)
        total_tokens = sum(abs(h.token_delta) for h in same)
        if total_quote and total_tokens:
            scaled = abs(total_quote) * share // total_tokens
            return asset, (scaled if total_quote > 0 else -scaled), _within_fee_tolerance(matched, total_tokens)
    elsewhere = [h for h in side if abs(abs(h.token_delta) - matched) <= AMOUNT_TOLERANCE_WEI]
    if elsewhere:
        h = min(elsewhere, key=lambda h: h.log_index)
        quote = _to_native_units(h.quote_asset, h.quote_delta, rates)
        scaled = abs(quote) * share // matched
        return h.quote_asset, (scaled if quote > 0 else -scaled), True
    dealt = [q for q in quotes_by_wallet.get(venue, []) if q.delta and (via is None or q.counterparty == via)]
    if not dealt:
        return None
    asset, received = _combine([(q.asset, _to_native_units(q.asset, q.delta, rates)) for q in dealt], rates)
    if received == 0 or (received > 0) != incoming:
        return None
    venue_tokens = sum(a for _, a in (paths.outbound if incoming else paths.inbound).get((token, venue), []))
    if venue_tokens <= 0:
        return None
    scaled = abs(received) * share // venue_tokens
    return asset, (-scaled if incoming else scaled), False


def _account_payment(
    paths: _Paths,
    quotes_by_wallet: dict[str, list[_QuoteMove]],
    token: str,
    via: str,
    party: str,
    share: int,
    incoming: bool,
    rates: Rates,
) -> tuple[str, int, bool] | None:
    """What the transparent address paid the wallet at the far end, or was paid by it, for these tokens."""
    paid = [
        q for q in quotes_by_wallet.get(via, []) if q.counterparty == party and q.delta and (q.delta < 0) == incoming
    ]
    if not paid:
        return None
    asset, total, _ = _quote_total(paid, rates)
    between = paths.between(token, via, party, incoming)
    if between <= 0 or total == 0:
        return None
    scaled = abs(total) * share // between
    return asset, (scaled if total > 0 else -scaled), True


@dataclass
class _End:
    party: str
    amount: int
    via: str | None
    quote: tuple[str, int, bool] | None
    priced_venue: bool


@dataclass
class _Share:
    """One holder's pro-rata slice of a venue's fills, kept so the slices can be made to sum exactly."""

    leg: _Leg
    venue: str
    tokens: int
    quote: int
    asset: str


def _settle_rounding(shares: list[_Share], hints: list[_Hint], rates: Rates) -> None:
    """Slices of one venue's fills sum to the fills' quote to the wei, whatever rounding did to each.

    Each holder's slice is floored on its own, so three equal slices of a quote of 10**18 + 1 came to a
    wei short of it. When the slices account for every token the venue filled, the largest slice takes the
    difference.
    """
    groups: dict[tuple[str, str, bool], list[_Share]] = defaultdict(list)
    for share in shares:
        groups[(share.venue, share.leg.token, share.leg.token_delta > 0)].append(share)
    for (venue, token, incoming), members in groups.items():
        same = [h for h in hints if h.venue == venue and h.token == token and (h.token_delta > 0) == incoming]
        if not same or len({s.asset for s in members}) != 1:
            continue
        asset, total_quote = _sum_hints(same, rates)
        if asset != members[0].asset or sum(s.tokens for s in members) != sum(abs(h.token_delta) for h in same):
            continue
        gap = total_quote - sum(s.quote for s in members)
        if gap == 0:
            continue
        largest = max(members, key=lambda s: (s.tokens, s.leg.wallet))
        if largest.leg.quote_asset != asset or largest.leg.quote_delta is None:
            continue
        largest.leg.quote_delta += gap
        largest.quote += gap


def _resolve_ends(
    wallet: str,
    token: str,
    incoming: bool,
    ends: list[tuple[str, int, str | None]],
    bundle: TxBundle,
    paths: _Paths,
    hints: list[_Hint],
    quotes_by_wallet: dict[str, list[_QuoteMove]],
    kinds: _Kinds,
    rates: Rates,
) -> list[_End]:
    venues = {h.venue for h in hints}
    out: list[_End] = []
    for party, share, via in ends:
        kind = kinds.of(party)
        quote = None
        priced_venue = False
        if kind in PRICE_VENUE_KINDS or party in venues:
            matched = max(paths.between(token, via or wallet, party, incoming), share)
            quote = _venue_quote(
                bundle, paths, party, token, share, matched, incoming, hints, quotes_by_wallet, via or wallet, rates
            )
            priced_venue = quote is not None
        elif via is not None and kinds.is_wallet(party):
            quote = _account_payment(paths, quotes_by_wallet, token, via, party, share, incoming, rates)
        out.append(_End(party, share, via, quote, priced_venue))
    return out


def _price_from_ends(leg: _Leg, ends: list[_End], kinds: _Kinds, rates: Rates, shares: list[_Share]) -> bool:
    """Price a movement by what the venues and wallets at the far end were paid. True when it could be.

    Tokens that trace to the zero address cost nothing and are counted at nothing. A portion that traces
    somewhere unpriced is carried at the price of the rest, and the result is an estimate.
    """
    parts: list[tuple[str, int]] = []
    covered = 0
    free = 0
    exact = True
    venues: dict[str, int] = defaultdict(int)
    for end in ends:
        if kinds.of(end.party) == KIND_ZERO:
            free += end.amount
            continue
        if end.quote is None:
            continue
        asset, delta, is_exact = end.quote
        parts.append((asset, delta))
        covered += end.amount
        exact = exact and is_exact
        if end.priced_venue:
            venues[end.party] += end.amount
            shares.append(_Share(leg, end.party, end.amount, delta, asset))
    if covered == 0:
        return False
    asset, delta = _combine(parts, rates)
    if delta == 0 or _same_sign(delta, leg.token_delta):
        return False
    missing = abs(leg.token_delta) - covered - free
    if missing > 0:
        scaled = abs(delta) * (covered + missing) // covered
        delta = scaled if delta > 0 else -scaled
        exact = False
    leg.quote_asset = asset
    leg.quote_delta = delta
    leg.source = SOURCE_VENUE_EVENT
    leg.basis_state = BASIS_OBSERVED if exact else BASIS_ESTIMATED
    mon, _ = _values(asset, delta, rates)
    leg.hint_price = _price(mon, leg.token_delta)
    if venues:
        leg.venue = max(venues.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return True


def _dominant(ends: list[_End], kinds: _Kinds) -> tuple[str | None, str | None]:
    """The party most of the tokens trace to, and the price venue among them if there is one."""
    if not ends:
        return None, None
    party = max(ends, key=lambda e: (e.amount, e.party)).party
    venues = [e for e in ends if kinds.of(e.party) in VENUE_KINDS]
    priced = [e for e in venues if kinds.of(e.party) in PRICE_VENUE_KINDS] or venues
    venue = max(priced, key=lambda e: (e.amount, e.party)).party if priced else None
    return party, venue


def _classify(leg: _Leg, bundle: TxBundle, kinds: _Kinds, origin: str | None, reference_price: PriceFn | None) -> None:
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


def _split_legs(move: _Move, ends: list[_End]) -> list[tuple[_Leg, list[_End]]]:
    """One movement becomes one leg per distinct destination.

    A hand-off through a distributor to several wallets names each of them, and a movement that was partly
    sold and partly given away books the two apart. Everything that traces to a venue, or to a wallet that
    was paid through the path, is one trade leg.
    """
    incoming = move.delta > 0
    traded = [e for e in ends if e.quote is not None]
    groups: list[list[_End]] = [traded] if traded else []
    by_party: dict[str, list[_End]] = {}
    for e in ends:
        if e.quote is None:
            by_party.setdefault(e.party, []).append(e)
    groups.extend(by_party.values())
    if len(groups) <= 1:
        return [(_Leg(move.wallet, move.token, move.delta, move.log_index, counterparty=move.counterparty), ends)]
    total = sum(e.amount for e in ends)
    whole = abs(move.delta)
    out: list[tuple[_Leg, list[_End]]] = []
    allocated = 0
    for i, group in enumerate(groups):
        amount = whole - allocated if i == len(groups) - 1 else whole * sum(e.amount for e in group) // total
        allocated += amount
        if amount <= 0:
            continue
        delta = amount if incoming else -amount
        out.append((_Leg(move.wallet, move.token, delta, move.log_index, counterparty=move.counterparty), group))
    return out


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
    """Net one transaction into flows: one per movement of a holder, per place it really went.

    A holder's own payment prices its movement, fees and all. Failing that, the movement is followed
    through every contract that only passed it on, and what the venues and wallets at the far end were paid
    is the price. Failing that too, it is a transfer, a mint, a burn or a deposit, and never a number that
    nothing on chain backs.
    """
    rates = rates or Rates()
    quote_assets = frozenset(a.lower() for a in quote_assets)
    tokens = {t.lower() for t, reg in registry.items() if reg.active and t.lower() not in quote_assets}
    kinds = _Kinds(kind_of)
    origin = (bundle.userop_sender or (bundle.meta.from_addr if bundle.meta else None) or "").lower() or None

    moves, quote_moves = _movements(bundle, tokens, quote_assets)
    extra_moves, extra_quotes, extra_hints = _venue_movements(bundle, tokens, pools, kinds)
    moves += extra_moves
    quote_moves += extra_quotes
    paths = _Paths(moves, kinds)
    actions = _match_actions(moves, quote_moves, kinds, paths, rates)
    if not actions:
        return []

    tx_tokens = {move.token for move, _ in actions}
    hints = _hints(bundle, tx_tokens, registry, quote_assets, kinds, markets) + extra_hints
    quotes_by_wallet: dict[str, list[_QuoteMove]] = defaultdict(list)
    for q in quote_moves:
        if q.delta:
            quotes_by_wallet[q.wallet].append(q)

    legs_by_wallet: dict[str, list[_Leg]] = defaultdict(list)
    all_legs: list[_Leg] = []
    shares: list[_Share] = []
    for move, assigned in actions:
        incoming = move.delta > 0
        raw_ends = paths.far_ends(move.token, move.counterparty, abs(move.delta), incoming)
        ends = _resolve_ends(
            move.wallet,
            move.token,
            incoming,
            raw_ends,
            bundle,
            paths,
            hints,
            quotes_by_wallet,
            kinds,
            rates,
        )
        own = _quote_total(assigned, rates) if assigned else None
        if own is not None and _same_sign(own[1], move.delta):
            own = None
        if own is not None:
            pieces = [(_Leg(move.wallet, move.token, move.delta, move.log_index, counterparty=move.counterparty), ends)]
        else:
            pieces = _split_legs(move, ends)
        for leg, leg_ends in pieces:
            party, venue = _dominant(leg_ends, kinds)
            leg.venue = venue
            if own is not None:
                leg.quote_asset, leg.quote_delta, leg.source = own
                leg.basis_state = BASIS_OBSERVED
                leg.kind = KIND_BUY if incoming else KIND_SELL
                leg.counterparty = venue or move.counterparty
            elif _price_from_ends(leg, leg_ends, kinds, rates, shares):
                leg.kind = KIND_BUY if incoming else KIND_SELL
                leg.counterparty = leg.venue or party or move.counterparty
            else:
                leg.counterparty = party or move.counterparty
            legs_by_wallet[move.wallet].append(leg)
            all_legs.append(leg)

    _settle_rounding(shares, hints, rates)
    _swap_pairs(legs_by_wallet, reference_price)
    for leg in all_legs:
        if leg.kind is None:
            _classify(leg, bundle, kinds, origin, reference_price)

    sub_index = _sub_indices(all_legs)
    all_legs.sort(key=lambda leg: (leg.wallet, leg.token, leg.log_index))
    return [_flow(bundle, leg, sub_index[id(leg)], origin, rates) for leg in all_legs]
