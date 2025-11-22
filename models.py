from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

@dataclass(slots=True)
class LaunchpadTrade:
    block_number: int
    timestamp: int
    token: str
    user: str
    is_buy: bool
    native_amount: int
    token_amount: int
    price_native: Decimal
    txhash: str

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
    
@dataclass(slots=True)
class AMMPool:
    market: str
    quote: str
    base: str
    marketType: int
    quoteDecimals: int
    baseDecimals: int
    quoteTicker: str
    baseTicker: str
    quoteName: str
    baseName: str
    feeBps: int
    reserveQuote: int = 0
    reserveBase: int = 0
    tvlUsd: Decimal = Decimal(0)
    volume24hUsd: Decimal = Decimal(0)
    fees24hUsd: Decimal = Decimal(0)
    apy24h: Decimal = Decimal(0)
    created: int = 0

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
    source: int = 0 # 0 is our launchpad, 1 is nadfun, 2 is printr
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

@dataclass(slots=True)
class LaunchpadUser:
    address: str
    tokens_created: int = 0
    tokens_graduated: int = 0
    total_realized_pnl_native: Decimal = Decimal(0)
    total_trades: int = 0

@dataclass(slots=True)
class LaunchpadPosition:
    user: str
    token: str
    token_bought: int = 0
    token_sold: int = 0
    native_spent: int = 0
    native_received: int = 0
    balance_token: int = 0
    realized_pnl_native: Decimal = Decimal(0)
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0