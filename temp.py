from sortedcontainers import SortedList
from typing import Dict, Tuple
from decimal import Decimal
import models

class State:
    def __init__(self) -> None:
        self.launchpad_tokens: Dict[str, models.LaunchpadToken] = {}
        self.launchpad_users: Dict[str, models.LaunchpadUser] = {}
        self.launchpad_positions: Dict[Tuple[str, str], models.LaunchpadPosition] = {}  # (token, user) -> position
        self.lp_token_holders_by_balance: Dict[str, SortedList] = {}  # token -> SortedList of positions
        self.lp_token_holders_by_pnl: Dict[str, SortedList] = {} # token -> SortedList of positions  
        self.lp_user_positions_by_pnl: Dict[str, SortedList] = {} # user -> SortedList of positions

    def _lp_unrealized_pnl(self, pos: models.LaunchpadPosition) -> Decimal:
        token_obj = self.launchpad_tokens.get(pos.token.lower())
        if not token_obj:
            return Decimal(0)
        
        current_value = Decimal(pos.balance_token) * token_obj.last_price_native
        total_out = Decimal(pos.native_received)
        total_in = Decimal(pos.native_spent)
        
        return pos.realized_pnl_native + current_value - total_in + total_out

    def apply_launchpad_position_update(self, pos: models.LaunchpadPosition) -> None:
        token = pos.token.lower()
        user = pos.user.lower()
        key = (token, user)
        
        if key in self.launchpad_positions:
            old_pos = self.launchpad_positions[key]
            
            if token in self.lp_token_holders_by_balance:
                self.lp_token_holders_by_balance[token].discard(old_pos)
            if token in self.lp_token_holders_by_pnl:
                self.lp_token_holders_by_pnl[token].discard(old_pos)
            if user in self.lp_user_positions_by_pnl:
                self.lp_user_positions_by_pnl[user].discard(old_pos)
        else:
            if token not in self.lp_token_holders_by_balance:
                self.lp_token_holders_by_balance[token] = SortedList(key=lambda p: -p.balance_token)
            if token not in self.lp_token_holders_by_pnl:
                self.lp_token_holders_by_pnl[token] = SortedList(key=lambda p: -self._lp_unrealized_pnl(p))
            if user not in self.lp_user_positions_by_pnl:
                self.lp_user_positions_by_pnl[user] = SortedList(key=lambda p: -self._lp_unrealized_pnl(p))
        
        self.launchpad_positions[key] = pos
        self.lp_token_holders_by_balance[token].add(pos)
        self.lp_token_holders_by_pnl[token].add(pos)
        self.lp_user_positions_by_pnl[user].add(pos)

    def lp_top_holders_by_balance(self, token: str, limit: int = 50) -> list[models.LaunchpadPosition]:
        token = token.lower()
        return list(self.lp_token_holders_by_balance.get(token, []))[:limit]

    def lp_top_holders_by_pnl(self, token: str, limit: int = 50) -> list[models.LaunchpadPosition]:
        token = token.lower()
        return list(self.lp_token_holders_by_pnl.get(token, []))[:limit]

    def lp_user_top_positions(self, user: str, limit: int = 50) -> list[models.LaunchpadPosition]:
        user = user.lower()
        return list(self.lp_user_positions_by_pnl.get(user, []))[:limit]

    def apply_launchpad_token_price_update(self, token: str, new_price: Decimal) -> None:
        token = token.lower()
        token_obj = self.launchpad_tokens.get(token)
        if not token_obj:
            return
        
        token_obj.last_price_native = new_price
        
        if token in self.lp_token_holders_by_pnl:
            positions = list(self.lp_token_holders_by_pnl[token])
            self.lp_token_holders_by_pnl[token] = SortedList(
                positions,
                key=lambda p: -self._lp_unrealized_pnl(p)
            )
            
            for pos in positions:
                user = pos.user.lower()
                if user in self.lp_user_positions_by_pnl:
                    user_positions = list(self.lp_user_positions_by_pnl[user])
                    self.lp_user_positions_by_pnl[user] = SortedList(
                        user_positions,
                        key=lambda p: -self._lp_unrealized_pnl(p)
                    )