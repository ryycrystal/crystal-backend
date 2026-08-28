from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.lifecycle import CurveState


@runtime_checkable
class LaunchpadAdapter(Protocol):
    source: int
    name: str

    def curve_state(self, ev: dict) -> CurveState | None: ...

    def initial_price_native(self) -> object | None: ...

    def graduates_to_market(self) -> bool: ...


_REGISTRY: dict[int, LaunchpadAdapter] = {}


def register(adapter: LaunchpadAdapter) -> LaunchpadAdapter:
    _REGISTRY[int(adapter.source)] = adapter
    return adapter


def get(source: int) -> LaunchpadAdapter | None:
    return _REGISTRY.get(int(source or 0))


def all_adapters() -> dict[int, LaunchpadAdapter]:
    return dict(_REGISTRY)
