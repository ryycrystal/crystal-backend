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
    
@dataclass(slots=True)
class Trade:
    block_number: int
    market: str
    user: str
    is_buy: bool
    amount_in: int
    amount_out: int
    start_price: int
    end_price: int
    
@dataclass(slots=True)
class VaultCreated:
    block_number: int
    vault: str
    quote: str
    base: str