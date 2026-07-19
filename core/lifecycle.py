"""Normalized token lifecycle.

A single internal representation that every launchpad integration maps into.
Source-specific geometry (virtual reserve offsets, supply splits, graduation
rules) lives in that source's adapter; nothing in this module may assume a
particular launchpad.

Lifecycle:

    created -> active -> graduating -> graduated [-> migrated]

    created     token exists, no trades observed yet
    active      trading on the bonding curve / vAMM
    graduating  past GRADUATING_THRESHOLD_BPS of the curve supply
    graduated   left the curve; trading routes to a market
    migrated    terminal state for sources that hand liquidity to an external
                venue after graduating (nad.fun). Native tokens never enter it:
                they graduate onto a Crystal market and stop there.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class TokenPhase(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    GRADUATING = "graduating"
    GRADUATED = "graduated"
    MIGRATED = "migrated"


# A token is "graduating" once this much of the curve supply has been sold.
GRADUATING_THRESHOLD_BPS = 7_500

BPS_DENOMINATOR = 10_000


@dataclass(frozen=True)
class CurveState:
    """Normalized bonding-curve state.

    Adapters are responsible for expressing their own geometry in these terms.
    ``tokens_sold`` and ``curve_supply`` are the only fields the lifecycle rules
    depend on, which keeps progress and phase identical across sources even when
    the underlying curves differ.

    All amounts are raw on-chain integers (no decimal scaling).
    """

    tokens_sold: int
    curve_supply: int
    native_reserve: int = 0
    token_reserve: int = 0

    @property
    def progress_bps(self) -> int:
        """Share of the curve supply sold, in basis points, clamped to [0, 10000]."""
        if self.curve_supply <= 0:
            return 0
        raw = (self.tokens_sold * BPS_DENOMINATOR) // self.curve_supply
        return max(0, min(BPS_DENOMINATOR, raw))

    @property
    def is_graduating(self) -> bool:
        return self.progress_bps >= GRADUATING_THRESHOLD_BPS

    @property
    def is_complete(self) -> bool:
        return self.curve_supply > 0 and self.tokens_sold >= self.curve_supply

    @property
    def spot_price_native(self) -> Decimal:
        """Native per whole token.

        Both reserves carry the same decimal scaling, so the ratio is already a
        per-whole-token price.
        """
        if self.native_reserve <= 0 or self.token_reserve <= 0:
            return Decimal(0)
        return Decimal(self.native_reserve) / Decimal(self.token_reserve)


@dataclass(frozen=True)
class LifecycleSnapshot:
    phase: TokenPhase
    progress_bps: int
    tokens_sold: int
    curve_supply: int

    @property
    def is_on_curve(self) -> bool:
        return self.phase in (TokenPhase.CREATED, TokenPhase.ACTIVE, TokenPhase.GRADUATING)

    @property
    def progress_ratio(self) -> Decimal:
        return Decimal(self.progress_bps) / Decimal(BPS_DENOMINATOR)


def resolve_phase(
    *,
    curve: CurveState | None,
    has_trades: bool,
    graduated: bool = False,
    migrated: bool = False,
) -> TokenPhase:
    """Terminal states win, then curve progress, then trade activity.

    ``graduated`` and ``migrated`` are facts reported by the adapter from chain
    events; they are never inferred from curve progress, because a curve can sit
    at 100% for a block before the graduation event is observed.
    """
    if migrated:
        return TokenPhase.MIGRATED
    if graduated:
        return TokenPhase.GRADUATED
    if curve is not None and curve.is_graduating:
        return TokenPhase.GRADUATING
    if has_trades:
        return TokenPhase.ACTIVE
    return TokenPhase.CREATED


def snapshot(
    *,
    curve: CurveState | None,
    has_trades: bool,
    graduated: bool = False,
    migrated: bool = False,
) -> LifecycleSnapshot:
    phase = resolve_phase(curve=curve, has_trades=has_trades, graduated=graduated, migrated=migrated)
    if curve is None:
        # off the curve (or not yet observed): a graduated token is fully sold
        done = phase in (TokenPhase.GRADUATED, TokenPhase.MIGRATED)
        return LifecycleSnapshot(
            phase=phase,
            progress_bps=BPS_DENOMINATOR if done else 0,
            tokens_sold=0,
            curve_supply=0,
        )
    return LifecycleSnapshot(
        phase=phase,
        progress_bps=curve.progress_bps,
        tokens_sold=curve.tokens_sold,
        curve_supply=curve.curve_supply,
    )
