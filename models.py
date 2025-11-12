from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

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
class Vault:
    vault: str
    quote: str
    base: str
    market: str
    owner: str
    name: str
    description: str
    social1: str
    social2: str
    social3: str
    locked: bool
    closed: bool
    maxShares: int
    circulatingShares: int
    quoteDecimals: int
    baseDecimals: int
    timestamp: int
    
@dataclass(slots=True)
class VaultBalance:
    quoteBalance: int
    baseBalance: int
    timestamp: int
    usdValue: Decimal
    
@dataclass(slots=True)
class VaultDeposit:
    user: str
    timestamp: int
    quoteAmount: int
    baseAmount: int
    shares: int
    hash: str
    
@dataclass(slots=True)
class VaultWithdraw:
    user: str
    timestamp: int
    quoteAmount: int
    baseAmount: int
    shares: int
    hash: str
    
@dataclass(slots=True)
class VaultUser:
    address: str
    vault: str
    shares: int
    deposits: int
    withdraws: int
    lastDeposit: int
    lastWithdraw: int
    
@dataclass(slots=True)
class MarketInfo:
    isCanonical: bool
    quoteAsset: str
    baseAsset: str
    market: str
    quoteAddress: str
    quoteDecimals: int
    quoteTicker: str
    quoteName: str
    baseAddress: str
    baseDecimals: int
    baseTicker: str
    baseName: str
    marketId: int
    marketType: int
    scaleFactor: int
    tickSize: int
    maxPrice: int
    minSize: int
    takerFee: int
    makerRebate: int
    price: float = 0.0