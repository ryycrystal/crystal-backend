from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

# launchpad trade
@dataclass(slots=True)
class LaunchpadTrade:
    block_number: int
    timestamp: int
    token: str
    user: str
    is_buy: bool
    native_amount: int
    token_amount: int
    usd_amount: int
    price_native: Decimal
    txhash: str

# launchpad token
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
    circulating_supply: int = 0
    snipers: int = 0

# summary of user
@dataclass(slots=True)
class LaunchpadUser:
    address: str
    tokens_created: int = 0
    tokens_graduated: int = 0
    total_realized_pnl_native: Decimal = Decimal(0)
    total_trades: int = 0

# per user per token position
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

# v3 pool (nadfun post-migration)
@dataclass(slots=True)
class PoolInfo:
    pool: str
    token_addr: str
    native_addr: str
    token_is_0: bool