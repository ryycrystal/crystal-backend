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
    native_reserve: int = 0
    token_reserve: int = 0

@dataclass(slots=True)
class TokenCreated:
    block_number: int
    token: str
    creator: str