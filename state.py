from __future__ import annotations
from typing import Dict, Any
from decimal import Decimal, getcontext
from collections import deque
import os
import httpx
import models
import core.storage as storage
import threading
from core import chain as h
from core import adapters as launchpad_adapters
from core.adapters import native as native_adapter_mod

import psycopg2

getcontext().prec = 100

INTERVALS = (1, 5, 15, 60, 300, 900, 3600, 14400, 86400)
LABEL = {300: "5m", 3600: "1h", 21600: "6h", 86400: "24h"}

RPC_HTTP = os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
LVMON = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"
NATIVE_EQUIV_QUOTES = {WMON, LVMON}
_STABLE_TICKERS = {"usd", "usdc", "usdt", "dai", "usde", "usdm"}
_ERC20_NAME_SELECTOR = "0x06fdde03"
_ERC20_SYMBOL_SELECTOR = "0x95d89b41"
_TOKEN_URI_SELECTOR = "0x3c130d90"
_GET_QUOTE_TOKEN_SELECTOR = "0x3df8a468"


def _abi_addr_arg(addr: str) -> str:
    return (addr or "").lower().removeprefix("0x").rjust(64, "0")


def _decode_abi_string(result: str | None) -> str:
    if not result or not isinstance(result, str) or result == "0x":
        return ""
    data_hex = result[2:] if result.startswith("0x") else result
    try:
        data = bytes.fromhex(data_hex)
    except ValueError:
        return ""
    if len(data) < 64:
        return ""
    try:
        offset = int.from_bytes(data[:32], "big")
        length = int.from_bytes(data[offset : offset + 32], "big")
        raw = data[offset + 32 : offset + 32 + length]
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _decode_abi_address(result: str | None) -> str:
    if not result or not isinstance(result, str) or result == "0x":
        return ""
    data_hex = result[2:] if result.startswith("0x") else result
    if len(data_hex) < 64:
        return ""
    return ("0x" + data_hex[-40:]).lower()


def _eth_call(to: str, data: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    resp = httpx.post(RPC_HTTP, json=payload, timeout=10.0)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(body)
    return body.get("result") or "0x"


def _fetch_token_string(token: str, selector: str) -> str:
    try:
        return _decode_abi_string(_eth_call(token, selector))
    except Exception:
        return ""


_LAUNCHPAD_PARAMS_SELECTOR = "0xfef2c170"
_LAUNCHPAD_PARAMS_CACHE: Dict[str, Any] = {}


def _fetch_launchpad_initial_native_supply() -> int:
    """launchpadParams().launchpadInitialNativeSupply (first return word).

    This is the curve's initial VIRTUAL native reserve, so the price of a freshly
    created token is V0 / INITIAL_TOKEN_SUPPLY. It is governance-settable, so it
    must be read rather than assumed.
    """
    cached = _LAUNCHPAD_PARAMS_CACHE.get("initial_native_supply")
    if cached is not None:
        return int(cached)
    value = 0
    try:
        res = _eth_call(h.CONTRACTS.get("ROUTER", ""), _LAUNCHPAD_PARAMS_SELECTOR)
        if isinstance(res, str) and res.startswith("0x") and len(res) >= 66:
            value = int(res[2:66], 16)
    except Exception:
        value = 0
    if value > 0:
        _LAUNCHPAD_PARAMS_CACHE["initial_native_supply"] = value
    return value


NATIVE_ADAPTER = native_adapter_mod.build(_fetch_launchpad_initial_native_supply)


_ERC20_DECIMALS_SELECTOR = "0x313ce567"


def _fetch_token_decimals(token: str) -> int | None:
    try:
        res = _eth_call(token, _ERC20_DECIMALS_SELECTOR)
        if isinstance(res, str) and res.startswith("0x") and len(res) > 2:
            d = int(res, 16)
            if 0 <= d <= 36:
                return d
    except Exception:
        pass
    return None


def _fetch_v2_quote_token(token: str) -> str:
    try:
        data = _GET_QUOTE_TOKEN_SELECTOR + _abi_addr_arg(token)
        return _decode_abi_address(_eth_call(h.NADFUN_V2_ADDR, data)) or WMON
    except Exception:
        return WMON

class State:
    def __init__(self) -> None:
        self._lock = threading.RLock()

        self.launchpad_tokens: Dict[str, models.LaunchpadToken] = {}
        self.launchpad_market_to_token: Dict[str, str] = {}
        self.v3_pools: Dict[str, models.PoolInfo] = {}
        self.token_to_v3_pool: Dict[str, str] = {}

        self.addressToMarket: Dict[str, models.MarketInfo] = {}
        self.tokenGraph: Dict[str, list[models.MarketInfo]] = {}
        self.tokenToPrice: Dict[str, Decimal] = {}

        self.allVaults: set[str] = set()
        self.vaults: Dict[str, models.Vault] = {}
        self.vaultBalancesDay: Dict[str, deque] = {}
        self.vaultBalancesWeek: Dict[str, deque] = {}
        self.vaultBalancesMonth: Dict[str, deque] = {}
        self.vaultBalancesAllTime: Dict[str, deque] = {}
        self.vaultToDeposits: Dict[str, list[models.VaultDeposit]] = {}
        self.vaultToWithdraws: Dict[str, list[models.VaultWithdraw]] = {}
        self.vaultToUsers: Dict[str, Dict[str, models.VaultUser]] = {}

        self._block_timestamps: Dict[int, int] = {}
        self._last_head_block: int | None = None
        self._last_head_ts: int | None = None

        self.mon_price_usd = Decimal("0.03")
        self._seed_aux_prices()
        
            
    def set_mon_price_usd(self, value) -> None:
        try:
            px = Decimal(value)
        except Exception:
            return

        if px <= 0:
            return

        self.mon_price_usd = px
        with self._lock:
            self.tokenToPrice[WMON] = px
            self.tokenToPrice[LVMON] = px

        try:
            storage.set_mon_price_usd(px)
        except Exception as e:
            print(f"[State] failed to persist mon_price_usd: {e!r}")
        
    def rebuild_from_db(self) -> None:
        with self._lock:
            self.launchpad_tokens.clear()
            self.launchpad_market_to_token.clear()
            self.v3_pools.clear()
            self.token_to_v3_pool.clear()
            self._reset_aux_locked()

            token_rows = storage.load_tokens_for_state()
            for row in token_rows:
                (
                    token,
                    creator,
                    name,
                    symbol,
                    metadata_cid,
                    description,
                    social1,
                    social2,
                    social3,
                    social4,
                    source,
                    created_block,
                    created_at,
                    migrated,
                    migrated_block,
                    migrated_at,
                    market,
                    last_price_native,
                    native_volume,
                    token_volume,
                    volume_usd,
                    fees_usd,
                    buy_count,
                    sell_count,
                    tx_count,
                    circulating_supply,
                    snipers_count,
                    approaching_75,
                    approaching_75_block,
                    approaching_75_at,
                    quote_token,
                    curve_native_reserve,
                    curve_token_reserve,
                ) = row

                lp = models.LaunchpadToken(
                    token=token,
                    creator=creator,
                    name=name,
                    symbol=symbol,
                    metadata_cid=metadata_cid,
                    description=description,
                    social1=social1,
                    social2=social2,
                    social3=social3,
                    social4=social4,
                    source=int(source),
                    created_block=int(created_block),
                    created_at=int(created_at),
                    migrated=bool(migrated),
                    migrated_block=int(migrated_block) if migrated_block is not None else None,
                    migrated_at=int(migrated_at) if migrated_at is not None else None,
                    market=market,
                    quote_token=(quote_token or WMON).lower(),
                    last_price_native=Decimal(last_price_native),
                    native_volume=int(native_volume),
                    token_volume=int(token_volume),
                    volume_usd=Decimal(volume_usd),
                    fees_usd=Decimal(fees_usd),
                    buy_count=int(buy_count),
                    sell_count=int(sell_count),
                    tx_count=int(tx_count),
                    circulating_supply=int(circulating_supply),
                    snipers=int(snipers_count),
                    approaching_75=bool(approaching_75),
                    approaching_75_block=int(approaching_75_block) if approaching_75_block is not None else 0,
                    approaching_75_at=int(approaching_75_at) if approaching_75_at is not None else 0,
                    curve_native_reserve=int(curve_native_reserve or 0),
                    curve_token_reserve=int(curve_token_reserve or 0),
                )

                self.launchpad_tokens[token.lower()] = lp
                if market:
                    self.launchpad_market_to_token[market.lower()] = token.lower()

            pool_rows = storage.load_all_pools()
            for pool, token_addr, native_addr, token_is_0 in pool_rows:
                pi = models.PoolInfo(
                    pool=pool.lower(),
                    token_addr=token_addr.lower(),
                    native_addr=native_addr.lower(),
                    token_is_0=bool(token_is_0),
                )
                self.v3_pools[pi.pool] = pi
                self.token_to_v3_pool[pi.token_addr] = pi.pool
                
                if pool.lower() not in h.ADDRS:
                    h.ADDRS.append(pool.lower())
                    
            try:
                stored = storage.get_mon_price_usd()
                if stored is not None and stored > 0:
                    self.mon_price_usd = Decimal(stored)
            except Exception as e:
                print(f"[State] Failed to load MON price from DB: {e!r}")
            self.tokenToPrice[WMON] = self.mon_price_usd
            self.tokenToPrice[LVMON] = self.mon_price_usd

            market_rows = storage.load_crystal_markets_for_state()
            for row in market_rows:
                (
                    market,
                    is_canonical,
                    quote_asset,
                    base_asset,
                    quote_address,
                    quote_decimals,
                    quote_ticker,
                    quote_name,
                    base_address,
                    base_decimals,
                    base_ticker,
                    base_name,
                    market_id,
                    market_type,
                    scale_factor,
                    tick_size,
                    max_price,
                    min_size,
                    taker_fee,
                    maker_rebate,
                    is_amm_enabled,
                    last_price,
                ) = row

                mi = models.MarketInfo(
                    isCanonical=bool(is_canonical),
                    isAMMEnabled=bool(is_amm_enabled),
                    quoteAsset=(quote_asset or "").lower(),
                    baseAsset=(base_asset or "").lower(),
                    market=(market or "").lower(),
                    quoteAddress=(quote_address or "").lower(),
                    quoteDecimals=int(quote_decimals or 0),
                    quoteTicker=quote_ticker or "",
                    quoteName=quote_name or "",
                    baseAddress=(base_address or "").lower(),
                    baseDecimals=int(base_decimals or 0),
                    baseTicker=base_ticker or "",
                    baseName=base_name or "",
                    marketId=int(market_id or 0),
                    marketType=int(market_type or 0),
                    scaleFactor=int(scale_factor or 0),
                    tickSize=int(tick_size or 0),
                    maxPrice=int(max_price or 0),
                    minSize=int(min_size or 0),
                    takerFee=int(taker_fee or 0),
                    makerRebate=int(maker_rebate or 0),
                    price=Decimal(last_price or 0),
                )
                if not mi.market:
                    continue
                self.addressToMarket[mi.market] = mi
                if mi.isCanonical:
                    self._replace_canonical_pair_market_locked(mi)
                    self._maybe_seed_stable_price_locked(mi.quoteAddress, mi.quoteTicker, mi.quoteName)
                    self._maybe_seed_stable_price_locked(mi.baseAddress, mi.baseTicker, mi.baseName)

            pool_state_rows = storage.load_crystal_pool_states_for_state()
            for row in pool_state_rows:
                (
                    market,
                    reserve_quote,
                    reserve_base,
                    total_shares,
                    tvl_usd,
                    volume_24h_usd,
                    fees_24h_usd,
                    apy_24h,
                    daily_yield_24h,
                    last_sync_block,
                    last_sync_at,
                ) = row
                mi = self.addressToMarket.get((market or "").lower())
                if mi is None:
                    continue
                mi.reserveQuote = int(reserve_quote or 0)
                mi.reserveBase = int(reserve_base or 0)
                mi.totalShares = int(total_shares or 0)
                mi.tvlUsd = Decimal(tvl_usd or 0)
                mi.volume24hUsd = Decimal(volume_24h_usd or 0)
                mi.fees24hUsd = Decimal(fees_24h_usd or 0)
                mi.apy24h = Decimal(apy_24h or 0)
                mi.dailyYield24h = Decimal(daily_yield_24h or 0)
                mi.lastSyncBlock = int(last_sync_block or 0)
                mi.lastSyncAt = int(last_sync_at or 0)

            self.sweep()

            vault_rows = storage.load_crystal_vaults_for_state()
            for row in vault_rows:
                (
                    vault,
                    quote,
                    base,
                    market,
                    owner,
                    name,
                    description,
                    social1,
                    social2,
                    social3,
                    locked,
                    closed,
                    max_shares,
                    circulating_shares,
                    quote_decimals,
                    base_decimals,
                    lockup,
                    decrease_on_withdraw,
                ) = row
                vaddr = (vault or "").lower()
                if not vaddr:
                    continue
                self.vaults[vaddr] = models.Vault(
                    vault=vaddr,
                    quote=(quote or "").lower(),
                    base=(base or "").lower(),
                    market=(market or "").lower(),
                    owner=(owner or "").lower(),
                    name=name or "",
                    description=description or "",
                    social1=social1 or "",
                    social2=social2 or "",
                    social3=social3 or "",
                    locked=bool(locked),
                    closed=bool(closed),
                    maxShares=int(max_shares or 0),
                    circulatingShares=int(circulating_shares or 0),
                    quoteDecimals=int(quote_decimals or 0),
                    baseDecimals=int(base_decimals or 0),
                    lockup=int(lockup or 0),
                    decreaseOnWithdraw=bool(decrease_on_withdraw),
                )
                self.allVaults.add(vaddr)
                self.vaultBalancesDay.setdefault(vaddr, deque())
                self.vaultBalancesWeek.setdefault(vaddr, deque())
                self.vaultBalancesMonth.setdefault(vaddr, deque())
                self.vaultBalancesAllTime.setdefault(vaddr, deque())
                self.vaultToDeposits.setdefault(vaddr, [])
                self.vaultToWithdraws.setdefault(vaddr, [])
                self.vaultToUsers.setdefault(vaddr, {})

            vault_user_rows = storage.load_crystal_vault_users_for_state()
            for vault, user_address, shares, deposits, withdraws, last_deposit, last_withdraw in vault_user_rows:
                vaddr = (vault or "").lower()
                if vaddr not in self.vaults:
                    continue
                uaddr = (user_address or "").lower()
                self.vaultToUsers.setdefault(vaddr, {})[uaddr] = models.VaultUser(
                    address=uaddr,
                    vault=vaddr,
                    shares=int(shares or 0),
                    deposits=int(deposits or 0),
                    withdraws=int(withdraws or 0),
                    lastDeposit=int(last_deposit or 0),
                    lastWithdraw=int(last_withdraw or 0),
                )

    def reset_for_reindex(self) -> None:
        with self._lock:
            self.launchpad_tokens.clear()
            self.launchpad_market_to_token.clear()
            self.v3_pools.clear()
            self.token_to_v3_pool.clear()
            self._reset_aux_locked()
            print(f"[State] Reset for reindex: cleared all in-memory state")

               
    def ensure_v2_launchpad_token(
        self,
        token: str,
        blk: int,
        ts: int,
        cur: psycopg2.extensions.cursor | None = None,
    ) -> bool:
        token = (token or "").lower()
        if not token:
            return False

        with self._lock:
            if token in self.launchpad_tokens:
                return True

        name = _fetch_token_string(token, _ERC20_NAME_SELECTOR)
        symbol = _fetch_token_string(token, _ERC20_SYMBOL_SELECTOR)
        token_uri = _fetch_token_string(token, _TOKEN_URI_SELECTOR)
        quote_token = _fetch_v2_quote_token(token)

        with self._lock:
            if token in self.launchpad_tokens:
                return True

            lp = models.LaunchpadToken(
                token=token,
                creator="",
                name=name,
                symbol=symbol,
                metadata_cid="",
                description="",
                social1="",
                social2="",
                social3="",
                social4="",
            )
            lp.created_block = int(blk)
            lp.created_at = int(ts or 0)
            lp.source = 1
            lp.quote_token = quote_token
            self.launchpad_tokens[token] = lp

            storage.upsert_token_created(
                token=token,
                creator="",
                name=name,
                symbol=symbol,
                metadata_cid="",
                description="",
                social1="",
                social2="",
                social3="",
                social4="",
                source=1,
                created_block=int(blk),
                created_at=int(ts or 0),
                last_price_native=lp.last_price_native,
                quote_token=quote_token,
                cur=cur,
            )

            storage.mark_nadfun_v2(token, cur=cur)

            if token_uri:
                try:
                    from modules import nadfun

                    nadfun.METADATA_QUEUE.append((token, token_uri))
                except Exception:
                    pass

            print(
                f"[State] discovered nad.fun v2 token {token} "
                f"name={name!r} symbol={symbol!r} quote={quote_token}",
                flush=True,
            )
            return True

                          
    def apply_token_created(self, blk: int, ev: dict, ts: int, log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> None:
        with self._lock:
            src = (log_addr or "").lower()
            if not (h.is_nadfun_address(src) or src == h.CONTRACTS.get("ROUTER", "").lower()):
                return
            
            token = ev.get("token","").lower()
            if not token:
                return
            
            creator = ev.get("creator", "").lower()
            name = ev.get("name", "")
            symbol = ev.get("symbol", "")
            metadata_cid = ev.get("metadata_cid", "")
            description = ev.get("description", "")
            social1 = ev.get("social1", "")
            social2 = ev.get("social2", "")
            social3 = ev.get("social3", "")
            social4 = ev.get("social4", "")
            source = int(ev.get("source", 0))
            quote_token = (ev.get("quote_token") or WMON).lower()
            
            if src == h.NADFUN_V2_ADDR:
                storage.mark_nadfun_v2(token, cur=cur)

            lp = self.launchpad_tokens.get(token)
            if lp is not None:
                if creator and not (lp.creator or ""):
                    lp.creator = creator
                    if name and not (lp.name or ""):
                        lp.name = name
                    if symbol and not (lp.symbol or ""):
                        lp.symbol = symbol
                    if metadata_cid and not (lp.metadata_cid or ""):
                        lp.metadata_cid = metadata_cid
                    lp.created_block = blk
                    lp.created_at = ts
                    storage.upsert_token_created(
                        token=token,
                        creator=creator,
                        name=lp.name,
                        symbol=lp.symbol,
                        metadata_cid=lp.metadata_cid,
                        description=lp.description,
                        social1=lp.social1,
                        social2=lp.social2,
                        social3=lp.social3,
                        social4=lp.social4,
                        source=lp.source,
                        created_block=blk,
                        created_at=ts,
                        last_price_native=lp.last_price_native,
                        quote_token=lp.quote_token,
                        cur=cur,
                    )
                    storage.increment_user_tokens_created(creator, cur=cur)
                return
            else:
                lp = models.LaunchpadToken(
                    token=token,
                    creator=creator,
                    name=name,
                    symbol=symbol,
                    metadata_cid=metadata_cid,
                    description=description,
                    social1=social1,
                    social2=social2,
                    social3=social3,
                    social4=social4,
                )
                lp.created_block = blk
                lp.created_at = ts
                lp.source = source
                lp.quote_token = quote_token
                self.launchpad_tokens[token] = lp
                
                if (source == 1):
                    lp.last_price_native = ev.get(
                        "last_price_native",
                        Decimal("0.00008387696")
                    )
                else:
                    adapter = launchpad_adapters.get(source)
                    if adapter is not None:
                        initial_price = adapter.initial_price_native()
                        if initial_price is not None and initial_price > 0:
                            lp.last_price_native = initial_price
                
            storage.upsert_token_created(
                token=token,
                creator=creator,
                name=name,
                symbol=symbol,
                metadata_cid=metadata_cid,
                description=description,
                social1=social1,
                social2=social2,
                social3=social3,
                social4=social4,
                source=source,
                created_block=blk,
                created_at=ts,
                last_price_native=lp.last_price_native,
                quote_token=lp.quote_token,
                cur=cur
            )
            
            if creator:
                storage.increment_user_tokens_created(creator, cur=cur)

            pool = (ev.get("pool") or "").lower()
            if source == 1 and pool and quote_token and token != quote_token:
                lp.market = pool
                lp.quote_token = quote_token
                token_is_0 = token < quote_token
                self.v3_pools[pool] = models.PoolInfo(
                    pool=pool,
                    token_addr=token,
                    native_addr=quote_token,
                    token_is_0=token_is_0,
                )
                self.token_to_v3_pool[token] = pool
                if pool not in h.ADDRS:
                    h.ADDRS.append(pool)
                storage.upsert_pool(
                    pool=pool,
                    token_addr=token,
                    native_addr=quote_token,
                    token_is_0=token_is_0,
                    cur=cur,
                )

                     
    def _ensure_native_launchpad_token_locked(self, token: str, blk: int, ts: int, cur=None):
        """Create a stub for a native launchpad token whose TokenCreated we never saw.

        Without this a LaunchpadTrade for an unknown token is dropped silently and
        permanently (backfill starting mid-range, a gap, or a reorg). A later
        TokenCreated backfills creator/name/symbol onto this stub.
        """
        tok = (token or "").lower()
        if not tok:
            return None

        name = _fetch_token_string(tok, _ERC20_NAME_SELECTOR)
        symbol = _fetch_token_string(tok, _ERC20_SYMBOL_SELECTOR)

        lp = models.LaunchpadToken(
            token=tok,
            creator="",
            name=name,
            symbol=symbol,
            metadata_cid="",
            description="",
            social1="",
            social2="",
            social3="",
            social4="",
        )
        lp.created_block = int(blk)
        lp.created_at = int(ts or 0)
        lp.source = 0
        lp.quote_token = WMON
        self.launchpad_tokens[tok] = lp

        try:
            storage.upsert_token_created(
                token=tok,
                creator="",
                name=name,
                symbol=symbol,
                metadata_cid="",
                description="",
                social1="",
                social2="",
                social3="",
                social4="",
                source=0,
                created_block=int(blk),
                created_at=int(ts or 0),
                last_price_native=lp.last_price_native,
                quote_token=WMON,
                cur=cur,
            )
        except Exception:
            pass

        print(
            f"[State] recovered native launchpad token {tok} from a trade at block {blk} "
            f"(TokenCreated not seen) name={name!r} symbol={symbol!r}",
            flush=True,
        )
        return lp

    def apply_launchpad_trade(self, ev: dict, blk: int, ts: int, txh: str, log_idx: int, _log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> None:
        with self._lock:
            # Trade rows dedupe on (txhash, log_index), but volume / tx_count /
            # volume_usd are running sums that would be advanced twice if the same
            # log were delivered again, inflating volume on any replay.
            if txh:
                try:
                    if storage.trade_exists(txh, log_idx, cur=cur):
                        return
                except Exception:
                    pass

            is_pool_swap = "pool" in ev and "amount0" in ev and "amount1" in ev
            if not is_pool_swap:
                token = ev.get("token", "").lower()
                user = ev.get("user", "").lower()
                if not token or not user:
                    return
                
                is_buy = bool(ev.get("is_buy", False))
                amount_in = int(ev.get("amount_in", 0) or 0)
                amount_out = int(ev.get("amount_out", 0) or 0)
                if amount_in <= 0 and amount_out <= 0:
                    return
                
                native_amt = amount_in if is_buy else amount_out
                token_amt = amount_out if is_buy else amount_in
                
                if native_amt <= 0 or token_amt <= 0:
                    return
                
                price_native = Decimal(0)
                try:
                    reserve_native = Decimal(int(ev.get("native_reserve") or 0))
                    reserve_token = Decimal(int(ev.get("token_reserve") or 0))
                    if reserve_native > 0 and reserve_token > 0:
                        price_native = reserve_native / reserve_token
                except Exception:
                    price_native = Decimal(0)
                if price_native <= 0:
                    try:
                        price_native = Decimal(native_amt) / Decimal(token_amt)
                    except Exception:
                        price_native = Decimal(0)
            else:
                pool_addr = (ev.get("pool") or "").lower()
                pi = self.v3_pools.get(pool_addr)
                if pi is None:
                    return

                token = (pi.token_addr or "").lower()
                user = (ev.get("user") or "").lower()

                if not token or not user:
                    return

                try:
                    amount0 = int(ev.get("amount0") or 0)
                    amount1 = int(ev.get("amount1") or 0)
                except Exception:
                    return

                if amount0 == 0 and amount1 == 0:
                    return

                if pi.token_is_0:
                    token_delta = amount0
                    native_delta = amount1
                else:
                    token_delta = amount1
                    native_delta = amount0

                if native_delta == 0 or token_delta == 0:
                    return

                is_buy = native_delta > 0

                if is_buy:
                    native_amt = native_delta
                    token_amt = -token_delta
                else:
                    native_amt = -native_delta
                    token_amt = token_delta

                if native_amt <= 0 or token_amt <= 0:
                    return

                is_buy_flag = is_buy
                amount_in = native_amt if is_buy_flag else token_amt
                amount_out = token_amt if is_buy_flag else native_amt

                price_raw = ev.get("sqrt_price_x96") or 0
                try:
                    if int(price_raw) <= 0:
                        price_native = Decimal(native_amt) / Decimal(token_amt)
                    else:
                        sqrt_p = Decimal(int(price_raw)) / Decimal(1 << 96)
                        ratio = sqrt_p * sqrt_p

                        if ratio <= 0:
                            price_native = Decimal(native_amt) / Decimal(token_amt)
                        else:
                            if pi.token_is_0:
                                price_native = ratio
                            else:
                                price_native = Decimal(1) / ratio
                except Exception:
                    price_native = Decimal(0)

            lp = self.launchpad_tokens.get(token)
            if lp is None:
                if is_pool_swap:
                    return
                lp = self._ensure_native_launchpad_token_locked(token, blk, ts, cur=cur)
                if lp is None:
                    return
            if is_pool_swap and not getattr(lp, "quote_token", ""):
                lp.quote_token = pi.native_addr or WMON
            
            lp.last_price_native = price_native
                
            if not is_pool_swap:
                if is_buy and blk <= lp.created_block + 10:
                    creator_addr = (lp.creator or "").lower()
                    user_addr = user.lower()
                    if user_addr and user_addr != creator_addr:
                        if batch is not None:
                            batch.add_sniper(token, user_addr)
                            lp.snipers += 1                                    
                        else:
                            inserted = storage.add_sniper_address(token, user_addr, cur=cur)
                            if inserted:
                                lp.snipers += 1

                adapter = launchpad_adapters.get(lp.source)
                curve = adapter.curve_state(ev) if adapter is not None else None

                if curve is not None:
                    # keep the last observed reserves so the next trade can derive
                    # its fee from the native delta after a restart
                    lp.curve_native_reserve = int(curve.native_reserve)
                    lp.curve_token_reserve = int(curve.token_reserve)
                    # normalized path: the adapter owns the curve geometry, the
                    # lifecycle model owns the progress/phase rules
                    lp.circulating_supply = curve.tokens_sold // 10 ** 18
                    if (not lp.approaching_75) and curve.is_graduating:
                        lp.approaching_75 = True
                        lp.approaching_75_block = blk
                        lp.approaching_75_at = ts
                    elif lp.approaching_75 and not curve.is_graduating:
                        lp.approaching_75 = False
                        lp.approaching_75_block = 0
                        lp.approaching_75_at = 0
                elif is_buy:
                    lp.circulating_supply += token_amt / 1e18
                else:
                    lp.circulating_supply -= token_amt / 1e18

                if curve is not None:
                    pass
                elif lp.source == 1:
                    if (not lp.approaching_75) and lp.circulating_supply >= 594_825_000:
                        lp.approaching_75 = True
                        lp.approaching_75_block = blk
                        lp.approaching_75_at = ts
                    elif (lp.approaching_75) and lp.circulating_supply < 594_825_000:
                        lp.approaching_75 = False
                        lp.approaching_75_block = 0
                        lp.approaching_75_at = 0
            
            lp.native_volume += native_amt
            lp.token_volume += token_amt
            lp.tx_count += 1
            if is_buy:
                lp.buy_count += 1
            else:
                lp.sell_count += 1

            quote_price = self._quote_price_usd(lp.quote_token)
            volume_usd_trade = Decimal(0)
            if quote_price > 0:
                volume_usd_trade = (Decimal(native_amt) / (Decimal(10) ** 18)) * quote_price
                lp.volume_usd += volume_usd_trade
                lp.fees_usd += volume_usd_trade * Decimal("0.01")

            if is_buy:
                token_bought_delta = int(token_amt)
                token_sold_delta = 0
                native_spent_delta = int(native_amt)
                native_received_delta = 0
                balance_token_delta = 0
                realized_delta = Decimal(-native_amt)
                buy_count_delta = 1
                sell_count_delta = 0
            else:
                token_bought_delta = 0
                token_sold_delta = int(token_amt)
                native_spent_delta = 0
                native_received_delta = int(native_amt)
                balance_token_delta = 0
                realized_delta = Decimal(native_amt)
                buy_count_delta = 0
                sell_count_delta = 1

            trade_count_delta = 1
            
            usd_amount = volume_usd_trade

            if batch is not None:
                                               
                batch.add_trade(
                    block_number=blk,
                    log_index=log_idx,
                    timestamp=ts,
                    token=token,
                    user_address=user,
                    is_buy=is_buy,
                    native_amount=int(native_amt),
                    token_amount=int(token_amt),
                    usd_amount=usd_amount,
                    price_native=lp.last_price_native,
                    txhash=txh,
                )
                batch.set_token_state(token, {
                    "last_price_native": lp.last_price_native,
                    "native_volume": int(lp.native_volume),
                    "token_volume": int(lp.token_volume),
                    "volume_usd": lp.volume_usd,
                    "fees_usd": lp.fees_usd,
                    "buy_count": lp.buy_count,
                    "sell_count": lp.sell_count,
                    "tx_count": lp.tx_count,
                    "circulating_supply": lp.circulating_supply,
                    "approaching_75": lp.approaching_75,
                    "approaching_75_block": lp.approaching_75_block,
                    "approaching_75_at": lp.approaching_75_at,
                    "snipers_count": lp.snipers,
                    "curve_native_reserve": int(lp.curve_native_reserve),
                    "curve_token_reserve": int(lp.curve_token_reserve),
                })
                batch.add_user_delta(user, int(native_amt), realized_delta)
                batch.add_position_delta(
                    user_address=user,
                    token=token,
                    token_bought_delta=token_bought_delta,
                    token_sold_delta=token_sold_delta,
                    native_spent_delta=native_spent_delta,
                    native_received_delta=native_received_delta,
                    balance_token_delta=balance_token_delta,
                    realized_pnl_delta=realized_delta,
                    trade_count_delta=trade_count_delta,
                    buy_count_delta=buy_count_delta,
                    sell_count_delta=sell_count_delta,
                    last_price_native=lp.last_price_native,
                )
                for bucket_seconds in INTERVALS:
                    bucket_start = (int(ts) // bucket_seconds) * bucket_seconds
                    batch.add_ohlcv(token, bucket_seconds, bucket_start, lp.last_price_native, int(native_amt))
            else:
                                                         
                storage.insert_trade(
                    block_number=blk,
                    log_index=log_idx,
                    timestamp=ts,
                    token=token,
                    user_address=user,
                    is_buy=is_buy,
                    native_amount=int(native_amt),
                    token_amount=int(token_amt),
                    usd_amount=usd_amount,
                    price_native=lp.last_price_native,
                    txhash=txh,
                    cur=cur,
                )
                storage.update_token_after_trade(
                    token=token,
                    last_price_native=lp.last_price_native,
                    native_volume=int(lp.native_volume),
                    token_volume=int(lp.token_volume),
                    volume_usd=lp.volume_usd,
                    fees_usd=lp.fees_usd,
                    buy_count=lp.buy_count,
                    sell_count=lp.sell_count,
                    tx_count=lp.tx_count,
                    circulating_supply=lp.circulating_supply,
                    approaching_75=lp.approaching_75,
                    approaching_75_block=lp.approaching_75_block,
                    approaching_75_at=lp.approaching_75_at,
                    snipers_count=lp.snipers,
                    curve_native_reserve=int(lp.curve_native_reserve),
                    curve_token_reserve=int(lp.curve_token_reserve),
                    cur=cur,
                )
                storage.update_user_on_trade(
                    address=user,
                    native_amount=int(native_amt),
                    realized_delta=realized_delta,
                    cur=cur,
                )

                storage.upsert_position(
                    user_address=user,
                    token=token,
                    token_bought_delta=token_bought_delta,
                    token_sold_delta=token_sold_delta,
                    native_spent_delta=native_spent_delta,
                    native_received_delta=native_received_delta,
                    balance_token_delta=balance_token_delta,
                    realized_pnl_delta=realized_delta,
                    trade_count_delta=trade_count_delta,
                    buy_count_delta=buy_count_delta,
                    sell_count_delta=sell_count_delta,
                    last_price_native=lp.last_price_native,
                    cur=cur,
                )

                for bucket_seconds in INTERVALS:
                    bucket_start = (int(ts) // bucket_seconds) * bucket_seconds
                    storage.upsert_ohlcv(
                        token=token,
                        resolution_sec=bucket_seconds,
                        bucket_start=bucket_start,
                        price_native=lp.last_price_native,
                        native_amount=int(native_amt),
                        cur=cur,
                    )

                                  
    def apply_migrated(self, blk: int, ts: int, ev: dict, log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> str | None:
        with self._lock:
            src = (log_addr or "").lower()
            if not (h.is_nadfun_address(src) or src == h.CONTRACTS.get("ROUTER", "").lower()):
                return None
            
            token = (ev.get("token") or "").lower()
            if not token:
                return None

            lp = self.launchpad_tokens.get(token)
            if lp is None:
                return None

            lp.migrated = True
            lp.migrated_block = blk
            lp.migrated_at = ts

            pool = (ev.get("pool") or "").lower()
            
            if pool and pool not in self.v3_pools:
                quote_token = (lp.quote_token or WMON).lower()

                lp.market = pool

                if token != quote_token:
                    token_is_0 = token < quote_token

                    self.v3_pools[pool] = models.PoolInfo(
                        pool=pool,
                        token_addr=token,
                        native_addr=quote_token,
                        token_is_0=token_is_0,
                    )
                    self.token_to_v3_pool[token] = pool
                    
                    storage.upsert_pool(
                        pool=pool,
                        token_addr=token,
                        native_addr=quote_token,
                        token_is_0=token_is_0,
                        cur=cur,
                    )
                    
            storage.mark_token_migrated(
                token=token,
                migrated_block=blk,
                migrated_at=ts,
                pool=pool or None,
                cur=cur,
            )
            
            if pool:
                storage.clear_position(user_address=pool, token=token, cur=cur)

            creator = lp.creator.lower() if lp.creator else ""
            if creator:               
                storage.increment_user_tokens_graduated(creator, cur=cur)

        return pool or None

                                        
    def apply_token_transfer(self, ev: dict, blk: int, ts: int, _log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> None:
        with self._lock:
            token = (ev.get("token") or "").lower()
            if not token:
                return

            lp = self.launchpad_tokens.get(token)
            if lp is None:
                return

            amount = int(ev.get("amount", 0) or 0)
            if amount <= 0:
                return

            from_addr = (ev.get("from") or "").lower()
            to_addr = (ev.get("to") or "").lower()

            zero = "0x" + "0" * 40
            internal = {a.lower() for a in getattr(h, "ADDRS", [])}

            def adjust(user: str, delta: int) -> None:
                addr = (user or "").lower()
                if not addr or addr == zero or delta == 0:
                    return
                if addr in internal:
                    return

                if batch is not None:
                    batch.add_position_delta(
                        user_address=addr,
                        token=token,
                        token_bought_delta=0,
                        token_sold_delta=0,
                        native_spent_delta=0,
                        native_received_delta=0,
                        balance_token_delta=int(delta),
                        realized_pnl_delta=Decimal(0),
                        trade_count_delta=0,
                        buy_count_delta=0,
                        sell_count_delta=0,
                        last_price_native=lp.last_price_native,
                    )
                else:
                    storage.upsert_position(
                        user_address=addr,
                        token=token,
                        token_bought_delta=0,
                        token_sold_delta=0,
                        native_spent_delta=0,
                        native_received_delta=0,
                        balance_token_delta=int(delta),
                        realized_pnl_delta=Decimal(0),
                        trade_count_delta=0,
                        buy_count_delta=0,
                        sell_count_delta=0,
                        last_price_native=lp.last_price_native,
                        cur=cur,
                    )

            adjust(from_addr, -amount)
            adjust(to_addr, amount)

    def _seed_aux_prices(self) -> None:
        with self._lock:
            self._seed_aux_prices_locked()

    def _seed_aux_prices_locked(self) -> None:
        self.tokenToPrice.clear()
        if self.mon_price_usd > 0:
            self.tokenToPrice[WMON] = self.mon_price_usd
            self.tokenToPrice[LVMON] = self.mon_price_usd

    def _quote_price_usd(self, quote_token: str) -> Decimal:
        quote = (quote_token or WMON).lower()
        if quote in NATIVE_EQUIV_QUOTES:
            return self.mon_price_usd if self.mon_price_usd > 0 else Decimal(0)
        return self.tokenToPrice.get(quote, Decimal(0))

    def _reset_aux_locked(self) -> None:
        self.addressToMarket.clear()
        self.tokenGraph.clear()
        self.allVaults.clear()
        self.vaults.clear()
        self.vaultBalancesDay.clear()
        self.vaultBalancesWeek.clear()
        self.vaultBalancesMonth.clear()
        self.vaultBalancesAllTime.clear()
        self.vaultToDeposits.clear()
        self.vaultToWithdraws.clear()
        self.vaultToUsers.clear()
        self._block_timestamps.clear()
        self._last_head_block = None
        self._last_head_ts = None
        self._seed_aux_prices_locked()

    def note_head(self, blk: int, ts: int | None) -> None:
        with self._lock:
            self._last_head_block = int(blk)
            if ts is not None:
                self._last_head_ts = int(ts)
                self._block_timestamps[int(blk)] = int(ts)

    def note_block_timestamp(self, blk: int, ts: int | None) -> None:
        if ts is None:
            return
        with self._lock:
            self._block_timestamps[int(blk)] = int(ts)

    def block_ts(self, block: int) -> int:
        with self._lock:
            t = self._block_timestamps.get(int(block))
            if t is not None:
                return int(t)
            return int(self._last_head_ts or 0)

    def head_block_and_ts(self) -> tuple[int | None, int | None]:
        with self._lock:
            return self._last_head_block, self._last_head_ts

    @staticmethod
    def _raw_log_index(raw_log: dict) -> int:
        li = raw_log.get("logIndex")
        return int(li, 16) if isinstance(li, str) else int(li or 0)

    def _maybe_seed_stable_price_locked(self, token: str, ticker: str, name: str) -> None:
        token_l = (token or "").lower()
        if not token_l:
            return
        t = (ticker or "").strip().lower()
        n = (name or "").strip().lower()
        if t in _STABLE_TICKERS or n in _STABLE_TICKERS or " usd" in n or n.startswith("usd"):
            self.tokenToPrice[token_l] = Decimal(1)

    @staticmethod
    def _same_pair_unordered(a_quote: str, a_base: str, b_quote: str, b_base: str) -> bool:
        aq = (a_quote or "").lower()
        ab = (a_base or "").lower()
        bq = (b_quote or "").lower()
        bb = (b_base or "").lower()
        if not aq or not ab or not bq or not bb:
            return False
        return (aq == bq and ab == bb) or (aq == bb and ab == bq)

    def _remove_market_from_token_graph_locked(self, market_addr: str) -> None:
        maddr = (market_addr or "").lower()
        if not maddr:
            return
        for tok in list(self.tokenGraph.keys()):
            items = self.tokenGraph.get(tok) or []
            kept = [mi for mi in items if (getattr(mi, "market", "") or "").lower() != maddr]
            if kept:
                self.tokenGraph[tok] = kept
            else:
                self.tokenGraph.pop(tok, None)

    def _add_market_to_token_graph_locked(self, mi: models.MarketInfo) -> None:
        if mi is None:
            return
        maddr = (getattr(mi, "market", "") or "").lower()
        if not maddr:
            return
        for tok in (((getattr(mi, "baseAddress", "") or "").lower()), ((getattr(mi, "quoteAddress", "") or "").lower())):
            if not tok:
                continue
            items = self.tokenGraph.setdefault(tok, [])
            if any(((getattr(x, "market", "") or "").lower() == maddr) for x in items):
                continue
            items.append(mi)

    def _replace_canonical_pair_market_locked(self, mi: models.MarketInfo) -> None:
        if mi is None or not bool(getattr(mi, "isCanonical", False)):
            return
        maddr = (getattr(mi, "market", "") or "").lower()
        qa = (getattr(mi, "quoteAddress", "") or "").lower()
        ba = (getattr(mi, "baseAddress", "") or "").lower()
        if not maddr or not qa or not ba:
            return
        for other in list(self.addressToMarket.values()):
            if other is None:
                continue
            om = (getattr(other, "market", "") or "").lower()
            if not om or om == maddr:
                continue
            if not bool(getattr(other, "isCanonical", False)):
                continue
            if not self._same_pair_unordered(qa, ba, getattr(other, "quoteAddress", ""), getattr(other, "baseAddress", "")):
                continue
            other.isCanonical = False
            self._remove_market_from_token_graph_locked(om)
        self._remove_market_from_token_graph_locked(maddr)
        self._add_market_to_token_graph_locked(mi)

    def apply_market_created(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        if not ev:
            return
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["ROUTER"].lower():
                return

            mi = models.MarketInfo(
                isCanonical=bool(ev.get("isCanonical", False)),
                isAMMEnabled=bool(int(ev.get("marketType", 0) or 0) > 1),
                quoteAsset=(ev.get("quoteAsset") or "").lower(),
                baseAsset=(ev.get("baseAsset") or "").lower(),
                market=(ev.get("market") or "").lower(),
                quoteAddress=(ev.get("quoteAddress") or "").lower(),
                quoteDecimals=int(ev.get("quoteDecimals", 0) or 0),
                quoteTicker=ev.get("quoteTicker", "") or "",
                quoteName=ev.get("quoteName", "") or "",
                baseAddress=(ev.get("baseAddress") or "").lower(),
                baseDecimals=int(ev.get("baseDecimals", 0) or 0),
                baseTicker=ev.get("baseTicker", "") or "",
                baseName=ev.get("baseName", "") or "",
                marketId=int(ev.get("marketId", 0) or 0),
                marketType=int(ev.get("marketType", 0) or 0),
                scaleFactor=int(ev.get("scaleFactor", 0) or 0),
                tickSize=int(ev.get("tickSize", 0) or 0),
                maxPrice=int(ev.get("maxPrice", 0) or 0),
                minSize=int(ev.get("minSize", 0) or 0),
                takerFee=int(ev.get("takerFee", 0) or 0),
                makerRebate=int(ev.get("makerRebate", 0) or 0),
                price=Decimal(0),
            )

            if not mi.market:
                return

            self.addressToMarket[mi.market] = mi

            for cand in ((mi.baseAddress or "").lower(), (mi.quoteAddress or "").lower()):
                if not cand or cand not in self.launchpad_tokens:
                    continue
                lp_tok = self.launchpad_tokens[cand]
                if (lp_tok.market or "").lower() != mi.market:
                    lp_tok.market = mi.market
                    try:
                        storage.update_launchpad_token_market(token=cand, market=mi.market, cur=cur)
                    except Exception:
                        pass
                self.launchpad_market_to_token[mi.market] = cand
                break

            if mi.isCanonical:
                self._replace_canonical_pair_market_locked(mi)
                self._maybe_seed_stable_price_locked(mi.quoteAddress, mi.quoteTicker, mi.quoteName)
                self._maybe_seed_stable_price_locked(mi.baseAddress, mi.baseTicker, mi.baseName)
                if mi.quoteAddress == WMON and self.mon_price_usd > 0:
                    self.tokenToPrice[WMON] = self.mon_price_usd
                if mi.baseAddress == WMON and self.mon_price_usd > 0:
                    self.tokenToPrice[WMON] = self.mon_price_usd

                for v in self.vaults.values():
                    if not self._same_pair_unordered(v.quote, v.base, mi.quoteAddress, mi.baseAddress):
                        continue
                    v.market = mi.market
                    if (v.quote or "").lower() == (mi.quoteAddress or "").lower() and (v.base or "").lower() == (mi.baseAddress or "").lower():
                        v.quoteDecimals = int(mi.quoteDecimals or 0)
                        v.baseDecimals = int(mi.baseDecimals or 0)
                    elif (v.quote or "").lower() == (mi.baseAddress or "").lower() and (v.base or "").lower() == (mi.quoteAddress or "").lower():
                        v.quoteDecimals = int(mi.baseDecimals or 0)
                        v.baseDecimals = int(mi.quoteDecimals or 0)

            storage.upsert_crystal_market(
                market=mi.market,
                is_canonical=mi.isCanonical,
                quote_asset=mi.quoteAsset,
                base_asset=mi.baseAsset,
                quote_address=mi.quoteAddress,
                quote_decimals=mi.quoteDecimals,
                quote_ticker=mi.quoteTicker,
                quote_name=mi.quoteName,
                base_address=mi.baseAddress,
                base_decimals=mi.baseDecimals,
                base_ticker=mi.baseTicker,
                base_name=mi.baseName,
                market_id=mi.marketId,
                market_type=mi.marketType,
                scale_factor=mi.scaleFactor,
                tick_size=mi.tickSize,
                max_price=mi.maxPrice,
                min_size=mi.minSize,
                taker_fee=mi.takerFee,
                maker_rebate=mi.makerRebate,
                is_amm_enabled=mi.isAMMEnabled,
                created_block=blk,
                created_at=ts,
                cur=cur,
            )
            if mi.isCanonical:
                storage.link_crystal_vaults_for_market(
                    quote=mi.quoteAddress,
                    base=mi.baseAddress,
                    market=mi.market,
                    quote_decimals=mi.quoteDecimals,
                    base_decimals=mi.baseDecimals,
                    updated_block=blk,
                    updated_at=ts,
                    cur=cur,
                )

    def apply_market_params_changed(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        if not ev:
            return
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["ROUTER"].lower():
                return
            market = (ev.get("market") or "").lower()
            if not market:
                return
            mi = self.addressToMarket.get(market)
            min_size = int(ev.get("minSize", 0) or 0)
            taker_fee = int(ev.get("takerFee", 0) or 0)
            maker_rebate = int(ev.get("makerRebate", 0) or 0)
            is_amm_enabled = bool(ev.get("isAMMEnabled", False))
            if mi is not None:
                mi.minSize = min_size
                mi.takerFee = taker_fee
                mi.makerRebate = maker_rebate
                mi.isAMMEnabled = is_amm_enabled
            storage.update_crystal_market_params(
                market,
                min_size=min_size,
                taker_fee=taker_fee,
                maker_rebate=maker_rebate,
                is_amm_enabled=is_amm_enabled,
                updated_block=blk,
                updated_at=ts,
                cur=cur,
            )

    def _record_graduated_launchpad_trade_locked(
        self, *, lp_addr: str, mi, ev: dict, blk: int, ts: int, txh: str, log_idx: int, cur, batch
    ) -> None:
        """Keep a graduated launchpad token's price, trades and volume continuous.

        Before this, a token that graduated stopped producing launchpad_trades rows
        entirely: total and windowed volume froze at the graduation block while the
        token kept trading on its Crystal market.

        The launchpad token is the market's base and WMON the quote, so a buy is
        quote-in/base-out -- the same convention the bonding-curve event uses.
        """
        lp = self.launchpad_tokens.get(lp_addr)
        if lp is None:
            return

        price = getattr(mi, "price", None)
        if price and price > 0:
            lp.last_price_native = price
            try:
                storage.update_launchpad_token_price(
                    token=lp_addr, last_price_native=price, cur=cur
                )
            except Exception:
                pass

        is_buy = bool(ev.get("is_buy", False))
        amount_in = int(ev.get("amount_in", 0) or 0)
        amount_out = int(ev.get("amount_out", 0) or 0)
        native_amt = amount_in if is_buy else amount_out
        token_amt = amount_out if is_buy else amount_in
        if native_amt <= 0 or token_amt <= 0:
            return

        lp.native_volume += native_amt
        lp.token_volume += token_amt
        lp.tx_count += 1
        if is_buy:
            lp.buy_count += 1
        else:
            lp.sell_count += 1

        usd_amount = Decimal(0)
        quote_price = self._quote_price_usd(lp.quote_token)
        if quote_price > 0:
            usd_amount = (Decimal(native_amt) / (Decimal(10) ** 18)) * quote_price
            lp.volume_usd += usd_amount

        user = (ev.get("user") or "").lower()
        token_state = {
            "last_price_native": lp.last_price_native,
            "native_volume": int(lp.native_volume),
            "token_volume": int(lp.token_volume),
            "volume_usd": lp.volume_usd,
            "fees_usd": lp.fees_usd,
            "buy_count": lp.buy_count,
            "sell_count": lp.sell_count,
            "tx_count": lp.tx_count,
            "circulating_supply": lp.circulating_supply,
            "approaching_75": lp.approaching_75,
            "approaching_75_block": lp.approaching_75_block,
            "approaching_75_at": lp.approaching_75_at,
            "snipers_count": lp.snipers,
            # unchanged post-graduation: the curve is gone, but the last observed
            # reserves must not be zeroed by this write
            "curve_native_reserve": int(lp.curve_native_reserve),
            "curve_token_reserve": int(lp.curve_token_reserve),
        }

        if batch is not None:
            batch.add_trade(
                block_number=blk,
                log_index=int(log_idx or 0),
                timestamp=ts,
                token=lp_addr,
                user_address=user,
                is_buy=is_buy,
                native_amount=int(native_amt),
                token_amount=int(token_amt),
                usd_amount=usd_amount,
                price_native=lp.last_price_native,
                txhash=txh or "",
            )
            batch.set_token_state(lp_addr, token_state)
        else:
            try:
                storage.insert_trade(
                    block_number=blk,
                    log_index=int(log_idx or 0),
                    timestamp=ts,
                    token=lp_addr,
                    user_address=user,
                    is_buy=is_buy,
                    native_amount=int(native_amt),
                    token_amount=int(token_amt),
                    usd_amount=usd_amount,
                    price_native=lp.last_price_native,
                    txhash=txh or "",
                    cur=cur,
                )
            except Exception:
                pass

    def apply_market_trade(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None, txh: str = "", log_idx: int = 0) -> None:
        if not ev:
            return
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["ROUTER"].lower():
                return

            market = (ev.get("market") or "").lower()
            mi = self.addressToMarket.get(market)
            if mi is None:
                return

            try:
                pf = int(mi.quoteDecimals) + int(mi.scaleFactor) - int(mi.baseDecimals)
                if pf < 0:
                    return
                end_price = int(ev.get("end_price", 0) or 0)
                if end_price > 0:
                    mi.price = Decimal(end_price) / (Decimal(10) ** pf)
                    storage.update_crystal_market_price(
                        market,
                        mi.price,
                        updated_block=blk,
                        updated_at=ts,
                        cur=cur,
                    )

            except Exception:
                return

            # Launchpad-owned markets only. launchpad_market_to_token is populated
            # solely from MarketCreated events whose base/quote is a known launchpad
            # token, so ordinary Crystal markets never reach this.
            lp_addr = self.launchpad_market_to_token.get(market)
            if lp_addr and (mi.baseAddress or "").lower() == lp_addr:
                self._record_graduated_launchpad_trade_locked(
                    lp_addr=lp_addr,
                    mi=mi,
                    ev=ev,
                    blk=blk,
                    ts=ts,
                    txh=txh,
                    log_idx=log_idx,
                    cur=cur,
                    batch=batch,
                )

        changed_tokens = self.sweep()
        if changed_tokens:
            self._refresh_pools_for_price_tokens(changed_tokens, blk=blk, ts=ts, cur=cur)

    @staticmethod
    def _pool_fee_rate(mi: models.MarketInfo) -> Decimal:
        try:
            mt = int(getattr(mi, "marketType", 0) or 0)
            if mt > 1:
                return Decimal("0.0025")
            tf = int(getattr(mi, "takerFee", 0) or 0)
            if tf <= 0 or tf > 100000:
                return Decimal(0)
            fr = (Decimal(100000) - Decimal(tf)) / Decimal(100000)
            return fr if fr > 0 else Decimal(0)
        except Exception:
            return Decimal(0)

    def _pool_effective_usd_prices_locked(
        self,
        mi: models.MarketInfo,
        reserve_quote: int,
        reserve_base: int,
    ) -> tuple[Decimal, Decimal]:
        qd = int(getattr(mi, "quoteDecimals", 0) or 0)
        bd = int(getattr(mi, "baseDecimals", 0) or 0)
        pq = Decimal(self.tokenToPrice.get((getattr(mi, "quoteAddress", "") or "").lower(), Decimal(0)) or 0)
        pb = Decimal(self.tokenToPrice.get((getattr(mi, "baseAddress", "") or "").lower(), Decimal(0)) or 0)
        q_units = Decimal(int(reserve_quote or 0)) / (Decimal(10) ** qd if qd >= 0 else Decimal(1))
        b_units = Decimal(int(reserve_base or 0)) / (Decimal(10) ** bd if bd >= 0 else Decimal(1))
        if q_units > 0 and b_units > 0:
            if pq > 0 and pb <= 0:
                pb = (q_units * pq) / b_units
            elif pb > 0 and pq <= 0:
                pq = (b_units * pb) / q_units
        return pq, pb

    def _pool_tvl_usd_locked(self, mi: models.MarketInfo, reserve_quote: int, reserve_base: int) -> Decimal:
        try:
            qd = int(getattr(mi, "quoteDecimals", 0) or 0)
            bd = int(getattr(mi, "baseDecimals", 0) or 0)
            pq, pb = self._pool_effective_usd_prices_locked(mi, reserve_quote, reserve_base)
            q_units = Decimal(int(reserve_quote or 0)) / (Decimal(10) ** qd if qd >= 0 else Decimal(1))
            b_units = Decimal(int(reserve_base or 0)) / (Decimal(10) ** bd if bd >= 0 else Decimal(1))
            tvl = (q_units * pq) + (b_units * pb)
            return tvl if tvl.is_finite() else Decimal(0)
        except Exception:
            return Decimal(0)

    def _pool_trade_volume_usd_locked(
        self,
        mi: models.MarketInfo,
        delta_quote_raw: int,
        delta_base_raw: int,
    ) -> Decimal:
        try:
            qd = int(getattr(mi, "quoteDecimals", 0) or 0)
            bd = int(getattr(mi, "baseDecimals", 0) or 0)
            pq, pb = self._pool_effective_usd_prices_locked(
                mi,
                int(getattr(mi, "reserveQuote", 0) or 0),
                int(getattr(mi, "reserveBase", 0) or 0),
            )
            q_usd = Decimal(0)
            b_usd = Decimal(0)
            if pq > 0:
                q_units = Decimal(abs(int(delta_quote_raw or 0))) / (Decimal(10) ** qd if qd >= 0 else Decimal(1))
                q_usd = q_units * pq
            if pb > 0:
                b_units = Decimal(abs(int(delta_base_raw or 0))) / (Decimal(10) ** bd if bd >= 0 else Decimal(1))
                b_usd = b_units * pb
            if q_usd > 0 and b_usd > 0:
                vol = (q_usd + b_usd) / Decimal(2)
            elif q_usd > 0:
                vol = q_usd
            else:
                vol = b_usd
            return vol if vol.is_finite() else Decimal(0)
        except Exception:
            return Decimal(0)

    def _refresh_pools_for_price_tokens(self, tokens, *, blk: int, ts: int, cur=None) -> None:
        token_set = {str(t or "").lower() for t in (tokens or set()) if str(t or "").strip()}
        if not token_set:
            return
        with self._lock:
            updates = []
            candidate_markets: dict[str, models.MarketInfo] = {}
            for tok in token_set:
                for mi in self.tokenGraph.get(tok, []):
                    if mi is None:
                        continue
                    maddr = (getattr(mi, "market", "") or "").lower()
                    if not maddr:
                        continue
                    candidate_markets[maddr] = mi

            for mi in candidate_markets.values():
                if mi is None or int(getattr(mi, "marketType", 0) or 0) <= 1:
                    continue
                market = (getattr(mi, "market", "") or "").lower()
                if not market:
                    continue
                qaddr = (getattr(mi, "quoteAddress", "") or "").lower()
                baddr = (getattr(mi, "baseAddress", "") or "").lower()
                if qaddr not in token_set and baddr not in token_set:
                    continue
                rq = int(getattr(mi, "reserveQuote", 0) or 0)
                rb = int(getattr(mi, "reserveBase", 0) or 0)
                if rq == 0 and rb == 0:
                    continue
                if Decimal(getattr(mi, "tvlUsd", Decimal(0)) or 0) > 0:
                    continue
                tvl_usd = self._pool_tvl_usd_locked(mi, rq, rb)
                if tvl_usd <= 0:
                    continue
                mi.tvlUsd = tvl_usd
                if tvl_usd > 0:
                    mi.dailyYield24h = Decimal(getattr(mi, "fees24hUsd", Decimal(0)) or 0) / tvl_usd
                    mi.apy24h = mi.dailyYield24h * Decimal(365)
                else:
                    mi.dailyYield24h = Decimal(0)
                    mi.apy24h = Decimal(0)
                updates.append(
                    (
                        market,
                        rq,
                        rb,
                        int(getattr(mi, "totalShares", 0) or 0),
                        mi.tvlUsd,
                        Decimal(getattr(mi, "volume24hUsd", Decimal(0)) or 0),
                        Decimal(getattr(mi, "fees24hUsd", Decimal(0)) or 0),
                        mi.apy24h,
                        mi.dailyYield24h,
                        int(getattr(mi, "lastSyncBlock", 0) or 0) or None,
                        int(getattr(mi, "lastSyncAt", 0) or 0) or None,
                    )
                )
            for (
                market,
                reserve_quote,
                reserve_base,
                total_shares,
                tvl_usd,
                volume_24h_usd,
                fees_24h_usd,
                apy_24h,
                daily_yield_24h,
                last_sync_block,
                last_sync_at,
            ) in updates:
                storage.upsert_crystal_pool_state(
                    market=market,
                    reserve_quote=reserve_quote,
                    reserve_base=reserve_base,
                    total_shares=total_shares,
                    tvl_usd=tvl_usd,
                    volume_24h_usd=volume_24h_usd,
                    fees_24h_usd=fees_24h_usd,
                    apy_24h=apy_24h,
                    daily_yield_24h=daily_yield_24h,
                    last_sync_block=last_sync_block,
                    last_sync_at=last_sync_at,
                    updated_block=int(blk or 0),
                    updated_at=int(ts or 0),
                    cur=cur,
                )

    def apply_pool_sync(
        self,
        blk: int,
        ts: int,
        txh: str,
        ev: dict,
        log_addr: str,
        *,
        log_idx: int = 0,
        sync_kind: str | None = None,
        cur=None,
        batch=None,
    ) -> None:
        if not ev:
            return
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["ROUTER"].lower():
                return

            market = (ev.get("market") or "").lower()
            mi = self.addressToMarket.get(market)
            if mi is None or int(getattr(mi, "marketType", 0) or 0) <= 1:
                return

            new_rq = int(ev.get("reserveQuote", 0) or 0)
            new_rb = int(ev.get("reserveBase", 0) or 0)
            prev_rq = int(getattr(mi, "reserveQuote", 0) or 0)
            prev_rb = int(getattr(mi, "reserveBase", 0) or 0)
            dq = new_rq - prev_rq
            db = new_rb - prev_rb

            kind = (sync_kind or "").strip().lower() or None
            if kind not in {"trade", "mint", "burn"}:
                if prev_rq == 0 and prev_rb == 0:
                    kind = "init"
                elif dq == 0 and db == 0:
                    kind = "other"
                elif dq > 0 and db > 0:
                    kind = "mint"
                elif dq < 0 and db < 0:
                    kind = "burn"
                elif (dq > 0 and db < 0) or (dq < 0 and db > 0):
                    kind = "trade"
                else:
                    kind = "other"

            volume_usd = Decimal(0)
            fees_usd = Decimal(0)
            if kind == "trade" and (prev_rq != 0 or prev_rb != 0):
                volume_usd = self._pool_trade_volume_usd_locked(mi, dq, db)
                fees_usd = volume_usd * self._pool_fee_rate(mi)
                if not fees_usd.is_finite():
                    fees_usd = Decimal(0)

            mi.reserveQuote = new_rq
            mi.reserveBase = new_rb
            mi.lastSyncBlock = int(blk or 0)
            mi.lastSyncAt = int(ts or 0)

            storage.insert_crystal_pool_sync_event(
                block_number=int(blk or 0),
                log_index=int(log_idx or 0),
                timestamp=int(ts or 0),
                market=market,
                txhash=(txh or ""),
                kind=kind,
                reserve_quote=new_rq,
                reserve_base=new_rb,
                prev_reserve_quote=(prev_rq if (prev_rq != 0 or prev_rb != 0) else None),
                prev_reserve_base=(prev_rb if (prev_rq != 0 or prev_rb != 0) else None),
                volume_usd=volume_usd,
                fees_usd=fees_usd,
                cur=cur,
            )

            win_row = storage.sum_crystal_pool_trade_metrics_since(
                market,
                since_ts=max(0, int(ts or 0) - 24 * 3600),
                cur=cur,
            )
            vol_24h = Decimal((win_row[0] if win_row and len(win_row) > 0 else 0) or 0)
            fees_24h = Decimal((win_row[1] if win_row and len(win_row) > 1 else 0) or 0)

            tvl_usd = self._pool_tvl_usd_locked(mi, new_rq, new_rb)
            mi.tvlUsd = tvl_usd
            mi.volume24hUsd = vol_24h
            mi.fees24hUsd = fees_24h

            storage.insert_crystal_pool_tvl_sample(
                market=market,
                block_number=int(blk or 0),
                log_index=int(log_idx or 0),
                timestamp=int(ts or 0),
                reserve_quote=new_rq,
                reserve_base=new_rb,
                tvl_usd=tvl_usd,
                txhash=(txh or ""),
                cur=cur,
            )

            avg_tvl_24h = Decimal(
                storage.time_weighted_avg_crystal_pool_tvl_since(
                    market,
                    since_ts=max(0, int(ts or 0) - 24 * 3600),
                    end_ts=int(ts or 0),
                    cur=cur,
                )
                or 0
            )
            denom_tvl = avg_tvl_24h if avg_tvl_24h > 0 else tvl_usd
            if denom_tvl > 0:
                mi.dailyYield24h = fees_24h / denom_tvl
                mi.apy24h = mi.dailyYield24h * Decimal(365)
            else:
                mi.dailyYield24h = Decimal(0)
                mi.apy24h = Decimal(0)

            storage.upsert_crystal_pool_state(
                market=market,
                reserve_quote=new_rq,
                reserve_base=new_rb,
                total_shares=int(getattr(mi, "totalShares", 0) or 0),
                tvl_usd=tvl_usd,
                volume_24h_usd=vol_24h,
                fees_24h_usd=fees_24h,
                apy_24h=mi.apy24h,
                daily_yield_24h=mi.dailyYield24h,
                last_sync_block=int(blk or 0),
                last_sync_at=int(ts or 0),
                updated_block=int(blk or 0),
                updated_at=int(ts or 0),
                cur=cur,
            )

    def apply_pool_transfer(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        if not ev:
            return
        with self._lock:
            token = (ev.get("token") or "").lower()
            mi = self.addressToMarket.get(token)
            if mi is None or int(getattr(mi, "marketType", 0) or 0) <= 1:
                return

            zero = "0x" + "0" * 40
            from_addr = (ev.get("from") or "").lower()
            to_addr = (ev.get("to") or "").lower()
            amount = int(ev.get("amount", 0) or 0)
            if amount < 0:
                amount = 0

            total = int(getattr(mi, "totalShares", 0) or 0)
            if from_addr == zero:
                total += amount
            elif to_addr == zero and from_addr != zero:
                total -= amount
                if total < 0:
                    total = 0
            mi.totalShares = total

            if from_addr and from_addr != zero:
                storage.upsert_crystal_pool_lp_user_delta(
                    market=token,
                    user_address=from_addr,
                    shares_delta=-amount,
                    last_transfer=int(ts or 0),
                    updated_block=int(blk or 0),
                    cur=cur,
                )
            if to_addr and to_addr != zero:
                storage.upsert_crystal_pool_lp_user_delta(
                    market=token,
                    user_address=to_addr,
                    shares_delta=amount,
                    last_transfer=int(ts or 0),
                    updated_block=int(blk or 0),
                    cur=cur,
                )

            storage.update_crystal_pool_total_shares(
                token,
                total_shares=total,
                updated_block=int(blk or 0),
                updated_at=int(ts or 0),
                cur=cur,
            )

    def sweep(self) -> set[str]:
        with self._lock:
            changed: set[str] = set()
            if self.mon_price_usd > 0:
                old_wmon = self.tokenToPrice.get(WMON)
                if old_wmon is None or Decimal(old_wmon) != self.mon_price_usd:
                    changed.add(WMON)
                self.tokenToPrice[WMON] = self.mon_price_usd

            q = deque([tok for tok, px in self.tokenToPrice.items() if px is not None and Decimal(px) > 0])
            visited: set[str] = set()

            while q:
                token = (q.popleft() or "").lower()
                if not token or token in visited:
                    continue
                visited.add(token)

                usd_q = self.tokenToPrice.get(token, Decimal(0))
                if usd_q is None or usd_q <= 0:
                    continue

                for mkt in self.tokenGraph.get(token, []):
                    qa = (getattr(mkt, "quoteAddress", "") or "").lower()
                    ba = (getattr(mkt, "baseAddress", "") or "").lower()
                    if qa != token:
                        continue

                    r = getattr(mkt, "price", None)
                    if r is None or Decimal(r) <= 0:
                        continue

                    new_px = Decimal(r) * Decimal(usd_q)
                    old_px = self.tokenToPrice.get(ba)
                    if old_px is None or Decimal(old_px) != new_px:
                        self.tokenToPrice[ba] = new_px
                        changed.add(ba)
                        q.append(ba)
            return changed

    def token_price(self, token: str) -> float:
        with self._lock:
            v = self.tokenToPrice.get((token or "").lower())
            return float(v) if v is not None else 0.0

    def apply_vault_deployed(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        if not ev:
            return
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return

            vaddr = (ev.get("vault") or "").lower()
            if not vaddr:
                return

            quote = (ev.get("quoteAsset") or "").lower()
            base = (ev.get("baseAsset") or "").lower()
            owner = (ev.get("owner") or "").lower()
            metadata = ev.get("metadata") or {}

            market = ""
            qd = 0
            bd = 0
            for mi in self.tokenGraph.get(quote, []):
                if not self._same_pair_unordered(quote, base, mi.quoteAddress, mi.baseAddress):
                    continue
                market = (mi.market or "").lower()
                if (mi.quoteAddress or "").lower() == quote and (mi.baseAddress or "").lower() == base:
                    qd = int(mi.quoteDecimals or 0)
                    bd = int(mi.baseDecimals or 0)
                else:
                    qd = int(mi.baseDecimals or 0)
                    bd = int(mi.quoteDecimals or 0)
                break

            if qd <= 0:
                d = _fetch_token_decimals(quote)
                if d is not None:
                    qd = d
            if bd <= 0:
                d = _fetch_token_decimals(base)
                if d is not None:
                    bd = d

            current = self.vaults.get(vaddr)
            circulating = int(getattr(current, "circulatingShares", 0) or 0)

            self.vaults[vaddr] = models.Vault(
                vault=vaddr,
                quote=quote,
                base=base,
                market=market,
                owner=owner,
                name=str(metadata.get("name") or ""),
                description=str(metadata.get("description") or ""),
                social1=str(metadata.get("social1") or ""),
                social2=str(metadata.get("social2") or ""),
                social3=str(metadata.get("social3") or ""),
                locked=bool(ev.get("locked", False)),
                closed=bool(ev.get("closed", False)),
                maxShares=int(ev.get("maxShares", 0) or 0),
                circulatingShares=circulating,
                quoteDecimals=qd,
                baseDecimals=bd,
                lockup=int(ev.get("lockup", 0) or 0),
                decreaseOnWithdraw=bool(ev.get("decreaseOnWithdraw", False)),
            )

            self.allVaults.add(vaddr)
            self.vaultBalancesDay.setdefault(vaddr, deque())
            self.vaultBalancesWeek.setdefault(vaddr, deque())
            self.vaultBalancesMonth.setdefault(vaddr, deque())
            self.vaultBalancesAllTime.setdefault(vaddr, deque())
            self.vaultToDeposits.setdefault(vaddr, [])
            self.vaultToWithdraws.setdefault(vaddr, [])
            self.vaultToUsers.setdefault(vaddr, {})

            vobj = self.vaults[vaddr]
            storage.upsert_crystal_vault(
                vault=vaddr,
                quote=vobj.quote,
                base=vobj.base,
                market=vobj.market,
                owner=vobj.owner,
                name=vobj.name,
                description=vobj.description,
                social1=vobj.social1,
                social2=vobj.social2,
                social3=vobj.social3,
                locked=vobj.locked,
                closed=vobj.closed,
                max_shares=vobj.maxShares,
                circulating_shares=vobj.circulatingShares,
                quote_decimals=vobj.quoteDecimals,
                base_decimals=vobj.baseDecimals,
                lockup=vobj.lockup,
                decrease_on_withdraw=vobj.decreaseOnWithdraw,
                deployed_block=blk,
                deployed_at=ts,
                updated_block=blk,
                updated_at=ts,
                cur=cur,
            )

    def apply_vault_deposit(self, blk: int, ts: int, txh: str, ev: dict, log_addr: str, cur=None, batch=None, log_idx: int = 0) -> None:
        if not ev:
            return
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vaddr = (ev.get("vault") or "").lower()
            if vaddr not in self.vaults:
                return

            user = (ev.get("sender") or "").lower()
            shares = int(ev.get("shares", 0) or 0)
            q_amt = int(ev.get("quoteAmount", 0) or 0)
            b_amt = int(ev.get("baseAmount", 0) or 0)

            self.vaultToDeposits.setdefault(vaddr, []).append(
                models.VaultDeposit(
                    user=user,
                    timestamp=int(ts),
                    quoteAmount=q_amt,
                    baseAmount=b_amt,
                    shares=shares,
                    hash=(txh or "").lower(),
                )
            )

            vobj = self.vaults[vaddr]
            prev_circulating = vobj.circulatingShares
            vobj.circulatingShares += shares

            users = self.vaultToUsers.setdefault(vaddr, {})
            u = users.get(user)
            if u is None:
                users[user] = models.VaultUser(
                    address=user,
                    vault=vaddr,
                    shares=max(0, shares),
                    deposits=1,
                    withdraws=0,
                    lastDeposit=int(ts),
                    lastWithdraw=0,
                )
            else:
                u.shares += shares
                if u.shares < 0:
                    u.shares = 0
                u.deposits += 1
                u.lastDeposit = int(ts)

            storage.insert_crystal_vault_deposit(
                block_number=blk,
                log_index=int(log_idx or 0),
                timestamp=ts,
                vault=vaddr,
                user_address=user,
                shares=shares,
                quote_amount=q_amt,
                base_amount=b_amt,
                txhash=(txh or ""),
                cur=cur,
            )
            storage.upsert_crystal_vault_user_delta(
                vault=vaddr,
                user_address=user,
                shares_delta=shares,
                deposits_delta=1,
                withdraws_delta=0,
                last_deposit=ts,
                last_withdraw=None,
                cur=cur,
            )
            storage.update_crystal_vault_fields(
                vault=vaddr,
                circulating_shares=vobj.circulatingShares,
                updated_block=blk,
                updated_at=ts,
                cur=cur,
            )

            if prev_circulating == 0 and shares > 0:
                try:
                    self.record_vault_balance_sample(vaddr, blk, ts, q_amt, b_amt)
                except Exception:
                    pass

    def apply_vault_withdraw(self, blk: int, ts: int, txh: str, ev: dict, log_addr: str, cur=None, batch=None, log_idx: int = 0) -> None:
        if not ev:
            return
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vaddr = (ev.get("vault") or "").lower()
            if vaddr not in self.vaults:
                return

            user = (ev.get("sender") or "").lower()
            shares = int(ev.get("shares", 0) or 0)
            q_amt = int(ev.get("quoteAmount", 0) or 0)
            b_amt = int(ev.get("baseAmount", 0) or 0)

            self.vaultToWithdraws.setdefault(vaddr, []).append(
                models.VaultWithdraw(
                    user=user,
                    timestamp=int(ts),
                    quoteAmount=q_amt,
                    baseAmount=b_amt,
                    shares=shares,
                    hash=(txh or "").lower(),
                )
            )

            vobj = self.vaults[vaddr]
            vobj.circulatingShares -= shares
            if vobj.circulatingShares < 0:
                vobj.circulatingShares = 0

            users = self.vaultToUsers.setdefault(vaddr, {})
            u = users.get(user)
            if u is None:
                users[user] = models.VaultUser(
                    address=user,
                    vault=vaddr,
                    shares=0,
                    deposits=0,
                    withdraws=1,
                    lastDeposit=0,
                    lastWithdraw=int(ts),
                )
            else:
                u.shares -= shares
                if u.shares < 0:
                    u.shares = 0
                u.withdraws += 1
                u.lastWithdraw = int(ts)

            storage.insert_crystal_vault_withdrawal(
                block_number=blk,
                log_index=int(log_idx or 0),
                timestamp=ts,
                vault=vaddr,
                user_address=user,
                shares=shares,
                quote_amount=q_amt,
                base_amount=b_amt,
                txhash=(txh or ""),
                cur=cur,
            )
            storage.upsert_crystal_vault_user_delta(
                vault=vaddr,
                user_address=user,
                shares_delta=-shares,
                deposits_delta=0,
                withdraws_delta=1,
                last_deposit=None,
                last_withdraw=ts,
                cur=cur,
            )
            storage.update_crystal_vault_fields(
                vault=vaddr,
                circulating_shares=vobj.circulatingShares,
                updated_block=blk,
                updated_at=ts,
                cur=cur,
            )

    def apply_vault_locked(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vaddr = (ev.get("vault") or "").lower()
            vobj = self.vaults.get(vaddr)
            if vobj is not None:
                vobj.locked = True
                storage.update_crystal_vault_fields(vault=vaddr, locked=True, updated_block=blk, updated_at=ts, cur=cur)

    def apply_vault_unlocked(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vaddr = (ev.get("vault") or "").lower()
            vobj = self.vaults.get(vaddr)
            if vobj is not None:
                vobj.locked = False
                storage.update_crystal_vault_fields(vault=vaddr, locked=False, updated_block=blk, updated_at=ts, cur=cur)

    def apply_vault_closed(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vaddr = (ev.get("vault") or "").lower()
            vobj = self.vaults.get(vaddr)
            if vobj is not None:
                vobj.closed = True
                storage.update_crystal_vault_fields(vault=vaddr, closed=True, updated_block=blk, updated_at=ts, cur=cur)

    def apply_vault_max_shares_changed(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vobj = self.vaults.get((ev.get("vault") or "").lower())
            if vobj is not None:
                vobj.maxShares = int(ev.get("maxShares", vobj.maxShares) or 0)
                storage.update_crystal_vault_fields(
                    vault=vobj.vault,
                    max_shares=vobj.maxShares,
                    updated_block=blk,
                    updated_at=ts,
                    cur=cur,
                )

    def apply_vault_lockup_changed(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vobj = self.vaults.get((ev.get("vault") or "").lower())
            if vobj is not None:
                vobj.lockup = int(ev.get("lockup", vobj.lockup) or 0)
                storage.update_crystal_vault_fields(
                    vault=vobj.vault,
                    lockup=vobj.lockup,
                    updated_block=blk,
                    updated_at=ts,
                    cur=cur,
                )

    def apply_vault_decrease_on_withdraw_changed(self, blk: int, ts: int, ev: dict, log_addr: str, cur=None, batch=None) -> None:
        with self._lock:
            if (log_addr or "").lower() != h.CONTRACTS["VAULTS"].lower():
                return
            vobj = self.vaults.get((ev.get("vault") or "").lower())
            if vobj is not None:
                vobj.decreaseOnWithdraw = bool(ev.get("decreaseOnWithdraw", vobj.decreaseOnWithdraw))
                storage.update_crystal_vault_fields(
                    vault=vobj.vault,
                    decrease_on_withdraw=vobj.decreaseOnWithdraw,
                    updated_block=blk,
                    updated_at=ts,
                    cur=cur,
                )

    @staticmethod
    def _prune_history(dq: deque, now_ts: int, horizon: int) -> None:
        cutoff = max(0, int(now_ts) - int(horizon))
        while dq and int(dq[0].get("timestamp", 0)) < cutoff:
            dq.popleft()

    def record_vault_balance_sample(self, vault: str, block: int, timestamp: int, quote_balance: int, base_balance: int) -> None:
        vaddr = (vault or "").lower()
        with self._lock:
            v = self.vaults.get(vaddr)
            if v is None:
                return

            qd = int(getattr(v, "quoteDecimals", 0) or 0)
            bd = int(getattr(v, "baseDecimals", 0) or 0)
            qaddr = (getattr(v, "quote", "") or "").lower()
            baddr = (getattr(v, "base", "") or "").lower()

            pq = Decimal(self.tokenToPrice.get(qaddr, Decimal(0)) or 0)
            pb = Decimal(self.tokenToPrice.get(baddr, Decimal(0)) or 0)

            q_units = Decimal(int(quote_balance)) / (Decimal(10) ** qd if qd >= 0 else Decimal(1))
            b_units = Decimal(int(base_balance)) / (Decimal(10) ** bd if bd >= 0 else Decimal(1))
            tvl_usd = (q_units * pq) + (b_units * pb)
            usd_value = float(tvl_usd) if tvl_usd.is_finite() else 0.0

            snap = {
                "block": int(block),
                "timestamp": int(timestamp),
                "quoteBalance": int(quote_balance),
                "baseBalance": int(base_balance),
                "usdValue": usd_value,
            }

            day = self.vaultBalancesDay.setdefault(vaddr, deque())
            week = self.vaultBalancesWeek.setdefault(vaddr, deque())
            month = self.vaultBalancesMonth.setdefault(vaddr, deque())
            alltm = self.vaultBalancesAllTime.setdefault(vaddr, deque())

            day.append(snap)
            week.append(snap)
            month.append(snap)
            alltm.append(snap)

            self._prune_history(day, int(timestamp), 24 * 3600)
            self._prune_history(week, int(timestamp), 7 * 24 * 3600)
            self._prune_history(month, int(timestamp), 30 * 24 * 3600)
            storage.upsert_crystal_vault_balance_sample(
                vault=vaddr,
                block_number=int(block),
                timestamp=int(timestamp),
                quote_balance=int(quote_balance),
                base_balance=int(base_balance),
                usd_value=Decimal(str(usd_value)),
            )

    def rebuild_aux_from_cached_logs(
        self,
        batch_size: int = 2000,
        start_block: int | None = None,
        reset: bool = True,
    ) -> int | None:
        if reset:
            with self._lock:
                self._reset_aux_locked()

        min_cached, max_cached = storage.get_cached_block_range()
        if min_cached is None or max_cached is None:
            return None

        if start_block is not None:
            if int(start_block) > int(max_cached):
                return int(max_cached)
            min_cached = max(int(min_cached), int(start_block))

        aux_tags = {"MC", "MPC", "TR"} | set(h.VAULT_FACTORY_EVENT_TAGS)

        for chunk_start in range(int(min_cached), int(max_cached) + 1, int(batch_size)):
            chunk_end = min(chunk_start + int(batch_size) - 1, int(max_cached))
            with storage.db_cursor() as cur:
                cached = storage.get_block_logs_range(chunk_start, chunk_end, cur=cur)

            for blk in range(chunk_start, chunk_end + 1):
                logs = sorted(cached.get(blk, []), key=self._raw_log_index)
                blk_ts = None
                for raw in logs:
                    raw_ts = raw.get("blockTimestamp")
                    if isinstance(raw_ts, str):
                        blk_ts = int(raw_ts, 16)
                        break
                    if raw_ts is not None:
                        blk_ts = int(raw_ts)
                        break
                if blk_ts is not None:
                    self.note_block_timestamp(blk, blk_ts)

                for raw in logs:
                    topics = raw.get("topics") or []
                    if not topics:
                        continue
                    tag = h.EVENT_SIGS.get(str(topics[0]).lower())
                    if tag not in aux_tags:
                        continue

                    addr = (raw.get("address") or "").lower()
                    h.register_dynamic_addresses_from_log(raw)
                    if not h.accepts_log_for_indexing(tag, addr):
                        continue

                    parser = h.PARSERS.get(tag)
                    if parser is None:
                        continue
                    try:
                        parsed = parser(addr, topics, (raw.get("data") or "")[2:])
                    except Exception:
                        continue

                    txh = (raw.get("transactionHash") or "").lower()
                    ts_eff = int(blk_ts or 0)

                    if tag == "MC":
                        self.apply_market_created(blk, ts_eff, parsed, addr)
                    elif tag == "MPC":
                        self.apply_market_params_changed(blk, ts_eff, parsed, addr)
                    elif tag == "TR":
                        self.apply_market_trade(blk, ts_eff, parsed, addr)
                    elif tag == "VD":
                        self.apply_vault_deployed(blk, ts_eff, parsed, addr)
                    elif tag == "VDP":
                        self.apply_vault_deposit(blk, ts_eff, txh, parsed, addr)
                    elif tag == "VWD":
                        self.apply_vault_withdraw(blk, ts_eff, txh, parsed, addr)
                    elif tag == "VLOCK":
                        self.apply_vault_locked(blk, ts_eff, parsed, addr)
                    elif tag == "VUNLOCK":
                        self.apply_vault_unlocked(blk, ts_eff, parsed, addr)
                    elif tag == "VCLOSE":
                        self.apply_vault_closed(blk, ts_eff, parsed, addr)
                    elif tag == "VMAX":
                        self.apply_vault_max_shares_changed(blk, ts_eff, parsed, addr)
                    elif tag == "VLOCKUP":
                        self.apply_vault_lockup_changed(blk, ts_eff, parsed, addr)
                    elif tag == "VDECR":
                        self.apply_vault_decrease_on_withdraw_changed(blk, ts_eff, parsed, addr)

            self.sweep()

        return int(max_cached)
