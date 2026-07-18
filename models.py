from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"

                 
@dataclass(slots=True)
class LaunchpadToken:
    token: str
    creator: str
    name: str
    symbol: str
    metadata_cid: str
    description: str
    social1: str
    social2: str
    social3: str
    social4: str
    created_block: int = 0
    created_at: int = 0
    migrated: bool = False
    migrated_block: int = 0
    migrated_at: int = 0
    market: str = ""
    source: int = 0                                               
    quote_token: str = WMON
    last_price_native: Decimal = Decimal(0.000001)
    native_volume: int = 0
    token_volume: int = 0
    volume_usd: Decimal = Decimal(0)
    fees_usd: Decimal = Decimal(0)
    buy_count: int = 0
    sell_count: int = 0
    tx_count: int = 0
    approaching_75: bool = False
    approaching_75_block: int = 0
    approaching_75_at: int = 0
    circulating_supply: int = 0
    snipers: int = 0
    # last observed curve reserves. Persisted so the previous native reserve
    # survives a restart: the per-trade fee rate is derived from its delta, and
    # reconstructing it from k = native * token is inexact (ceiling division).
    curve_native_reserve: int = 0
    curve_token_reserve: int = 0

                                 
@dataclass(slots=True)
class PoolInfo:
    pool: str
    token_addr: str
    native_addr: str
    token_is_0: bool

@dataclass(slots=True)
class MarketInfo:
    isCanonical: bool
    isAMMEnabled: bool
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
    price: Decimal = Decimal(0)
    reserveQuote: int = 0
    reserveBase: int = 0
    totalShares: int = 0
    tvlUsd: Decimal = Decimal(0)
    volume24hUsd: Decimal = Decimal(0)
    fees24hUsd: Decimal = Decimal(0)
    apy24h: Decimal = Decimal(0)
    dailyYield24h: Decimal = Decimal(0)
    lastSyncBlock: int = 0
    lastSyncAt: int = 0

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
    lockup: int = 0
    decreaseOnWithdraw: bool = False

@dataclass(slots=True)
class VaultBalance:
    quoteBalance: int
    baseBalance: int
    timestamp: int
    usdValue: Decimal
    block: int = 0

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
