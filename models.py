from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

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

# v3 pool (nadfun post-migration)
@dataclass(slots=True)
class PoolInfo:
    pool: str
    token_addr: str
    native_addr: str
    token_is_0: bool