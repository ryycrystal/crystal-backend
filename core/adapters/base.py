"""Launchpad adapter contract.

An adapter translates one launchpad's raw, source-specific event payloads into
the normalized representation in ``core.lifecycle``. All source-specific
constants, virtual-reserve offsets and graduation rules belong here -- never in
the lifecycle model, the indexer state, or the API.

To add a launchpad: implement this protocol and register it. Nothing downstream
should need to learn a new special case.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.lifecycle import CurveState


@runtime_checkable
class LaunchpadAdapter(Protocol):
    #: value stored in launchpad_tokens.source
    source: int

    #: human-readable identifier, for logs and diagnostics
    name: str

    def curve_state(self, ev: dict) -> CurveState | None:
        """Normalize a trade event into curve state.

        Returns ``None`` when the event carries no usable curve state (for
        example a post-graduation swap on an external venue).
        """
        ...

    def initial_price_native(self) -> object | None:
        """Spot price of a freshly created token, before any trade.

        ``None`` when the source cannot determine it. Returned as ``Decimal``.
        """
        ...

    def graduates_to_market(self) -> bool:
        """True when graduation routes trading to an internal Crystal market.

        Sources that hand liquidity to an external venue return False and reach
        ``TokenPhase.MIGRATED`` instead.
        """
        ...


_REGISTRY: dict[int, LaunchpadAdapter] = {}


def register(adapter: LaunchpadAdapter) -> LaunchpadAdapter:
    _REGISTRY[int(adapter.source)] = adapter
    return adapter


def get(source: int) -> LaunchpadAdapter | None:
    return _REGISTRY.get(int(source or 0))


def all_adapters() -> dict[int, LaunchpadAdapter]:
    return dict(_REGISTRY)
