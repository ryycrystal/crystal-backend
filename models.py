from __future__ import annotations
from dataclasses import dataclass

@dataclass(slots=True)
class LaunchpadTrade:
    block_number: int
    token: str
    user: str
    is_buy: bool
    amount_in: int
    amount_out: int

@dataclass(slots=True)
class TokenCreated:
    block_number: int
    token: str
    creator: str