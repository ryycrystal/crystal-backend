from __future__ import annotations
from typing import Dict, List
from decimal import Decimal, getcontext
import models

getcontext().prec = 50

INTERVALS = (300, 3600, 21600, 86400)
LABEL = {300: "5m", 3600: "1h", 21600: "6h", 86400: "24h"}

RPC_HTTP = "https://testnet-rpc.monad.xyz"

class State:
    def __init__(self) -> None:
        # launchpad
        self.launchpad_tokens: Dict[str, models.LaunchpadToken] = {} # tokenAddress -> LaunchpadToken
        self.launchpad_users: Dict[str, models.LaunchpadUser] = {} # userAddress -> LaunchpadUser
        self.launchpad_positions: Dict[tuple[str, str], models.LaunchpadPosition] = {} # [userAddress, tokenAddress] -> LaunchpadPosition
        self.launchpad_market_to_token: Dict[str, str] = {} # market/pool -> tokenAddress
        self.launchpad_trades: Dict[str, List[models.LaunchpadTrade]] = {} # tokenAddress -> LaunchpadTrade[]

        # graduated launchpad
        self.v3_pools: Dict[str, models.PoolInfo] = {} # poolAddress -> PoolInfo
        self.token_to_v3_pool: Dict[str, str] = {} # tokenAddress -> poolAddress

    # launchpad

    # apply token creation
    def apply_token_created(self, blk: int, ev: dict, ts: int, _log_addr: str) -> None:
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
        
        lp = self.launchpad_tokens.get(token)
        if lp is not None:
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
            self.launchpad_tokens[token] = lp
            
            if (source == 1):
                lp.last_price_native = ev.get("last_price_native", Decimal("0.00008387696"))
        
        if creator:
            u = self.launchpad_users.get(creator)
            if u is None:
                u = models.LaunchpadUser(address=creator)
                self.launchpad_users[creator] = u
            u.tokens_created += 1

    # applies a trade       
    def apply_launchpad_trade(self, ev: dict, blk: int, ts: int, txh: str, _log_addr: str) -> None:  
        is_v3_swap = "pool" in ev and "amount0" in ev and "amount1" in ev
        if not is_v3_swap:
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
            
            try:
                price_native = Decimal(ev.get("native_reserve")) / Decimal(ev.get("token_reserve"))
            except Exception:
                try:
                    price_native = Decimal(native_amt) / Decimal(token_amt)
                except Exception:
                    price_native = Decimal(0)
        else:
            pool_addr = (ev.get("pool") or "").lower()
            pi = self.v3_pools.get(pool_addr)
            if pi is None:
                return

            token = (pi.token or "").lower()
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
                sqrt_p = Decimal(int(price_raw))
                ratio = (sqrt_p * sqrt_p) / (Decimal(2) ** 192)
                if ratio <= 0:
                    price_native = Decimal(0)
                else:
                    if pi.token_is_0:
                        price_native = ratio
                    else:
                        price_native = Decimal(1) / ratio
            except Exception:
                price_native = Decimal(0)

        lp = self.launchpad_tokens.get(token)
        if lp is None:
            return
        
        lp.last_price_native = price_native
            
        if not is_v3_swap:
            if is_buy:
                lp.circulating_supply += token_amt / 1e18
            else:
                lp.circulating_supply -= token_amt / 1e18
        
            if lp.source == 0:
                if (not lp.approaching_75) and ev.get("native_reserve") >= 2_500_000_000_000_000_000_000:
                    lp.approaching_75 = True
                    lp.approaching_75_block = blk
                    lp.approaching_75_at = ts
                elif (lp.approaching_75) and ev.get("native_reserve") < 2_500_000_000_000_000_000_000:
                    lp.approaching_75 = False
                    lp.approaching_75_block = 0
                    lp.approaching_75_at = 0
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

        mon_price = Decimal(0.05)
        if mon_price > 0:
            volume_usd_trade = (Decimal(native_amt) / (Decimal(10) ** 18)) * mon_price
            lp.volume_usd += volume_usd_trade
            lp.fees_usd += volume_usd_trade * Decimal("0.01")

        lu = self.launchpad_users.get(user)
        if lu is None:
            lu = models.LaunchpadUser(address=user)
            self.launchpad_users[user] = lu
        lu.total_trades += 1

        key = (user, token)
        pos = self.launchpad_positions.get(key)
        if pos is None:
            pos = models.LaunchpadPosition(user=user, token=token)
            self.launchpad_positions[key] = pos

        pos.trade_count += 1
        if is_buy:
            pos.buy_count += 1
            pos.token_bought += token_amt
            pos.native_spent += native_amt
        else:
            pos.sell_count += 1
            pos.token_sold += token_amt
            pos.native_received += native_amt

        old_realized = pos.realized_pnl_native
        realized_native = pos.native_received - pos.native_spent
        pos.realized_pnl_native = Decimal(realized_native)

        delta = pos.realized_pnl_native - old_realized
        lu.total_realized_pnl_native += delta
        
        trades = self.launchpad_trades.setdefault(token, [])
        trades.append(
            models.LaunchpadTrade(
                block_number=blk,
                timestamp=int(ts),
                token=token,
                user=user,
                is_buy=is_buy,
                native_amount=int(native_amt),
                token_amount=int(token_amt),
                usd_amount=Decimal(native_amt) * Decimal(0.05),
                price_native=lp.last_price_native,
                txhash=txh
            )
        )
        if len(trades) > 500000:
            trades[:] = trades[-500000:]

    # applies graduation/migration
    def apply_migrated(self, blk: int, ts: int, ev: dict, _log_addr: str) -> str | None:
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
            wmon = "0x760afe86e5de5fa0ee542fc7b7b713e1c5425701".lower()

            if token != wmon:
                token_is_0 = token < wmon

                self.v3_pools[pool] = models.PoolInfo(
                    pool=pool,
                    token_addr=token,
                    native_addr=wmon,
                    token_is_0=token_is_0,
                )
                self.token_to_v3_pool[token] = pool

        creator = lp.creator.lower() if lp.creator else ""
        if creator:
            u = self.launchpad_users.get(creator)
            if u is None:
                u = models.LaunchpadUser(address=creator)
                self.launchpad_users[creator] = u
            u.tokens_graduated += 1

        return pool or None

    # keeps track of txfers for balances
    def apply_token_transfer(self, ev: dict, blk: int, ts: int, _log_addr: str) -> None:
        token = (ev.get("token") or "").lower()
        if not token:
            return

        if token not in self.launchpad_tokens:
            return

        amount = int(ev.get("amount", 0) or 0)
        if amount <= 0:
            return

        from_addr = (ev.get("from") or "").lower()
        to_addr = (ev.get("to") or "").lower()

        zero = "0x" + "0" * 40

        def adjust(user: str, delta: int) -> None:
            if not user or user == zero:
                return
            
            key = (user, token)
            pos = self.launchpad_positions.get(key)
            if pos is None:
                pos = models.LaunchpadPosition(user=user, token=token)
                self.launchpad_positions[key] = pos
            pos.balance_token += delta
            if pos.balance_token < 0:
                pos.balance_token = 0

        adjust(from_addr, -amount)
        adjust(to_addr, amount)