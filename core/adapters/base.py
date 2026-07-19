# launchpad adapter contract
#
# an adapter translates one launchpad's raw source specific event payloads into
# the normalized representation in core.lifecycle, all source specific constants,
# virtual reserve offsets and graduation rules belong here, never in the
# lifecycle model, the indexer state, or the api
#
# to add a launchpad implement this protocol and register it, nothing downstream
# should need to learn a new special case

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.lifecycle import CurveState


# the interface every launchpad source must implement
@runtime_checkable
class LaunchpadAdapter(Protocol):
    source: int
    name: str

    # normalize a trade event into curve state, none when it carries no curve
    def curve_state(self, ev: dict) -> CurveState | None: ...

    # spot price of a freshly created token as a decimal, none if unknown
    def initial_price_native(self) -> object | None: ...

    # true when graduation routes trading to an internal crystal market
    def graduates_to_market(self) -> bool: ...


_REGISTRY: dict[int, LaunchpadAdapter] = {}


# add an adapter to the source registry
def register(adapter: LaunchpadAdapter) -> LaunchpadAdapter:
    _REGISTRY[int(adapter.source)] = adapter
    return adapter


# look up the adapter for a source id
def get(source: int) -> LaunchpadAdapter | None:
    return _REGISTRY.get(int(source or 0))


# every registered adapter keyed by source id
def all_adapters() -> dict[int, LaunchpadAdapter]:
    return dict(_REGISTRY)
