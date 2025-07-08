from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Tuple

"""
in-memory state management for obs + “points” logic

‼️ thread-safety: just like before, all public methods assume they’re called
from the same asyncio thread. no wall‐clock time functions are used – only
block numbers → wall time via BLOCK_SECONDS.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, getcontext
import heapq
from typing import Dict, List, Optional

import helpers as h
import models

BLOCK_SECONDS = Decimal("0.5")

getcontext().prec = 50


def _multiplier(price: Decimal, mid: Decimal) -> Decimal:
    if mid == 0:
        return Decimal(0)
    d = abs(price - mid)
    m = Decimal(1) - (d / mid)
    return m if m > 0 else Decimal(0)


def _points_rate(size: Decimal, price: Decimal, mid: Decimal) -> Decimal:
    return size * _multiplier(price, mid) / 100


@dataclass(slots=True)
class _Order:
    side: str
    price: Decimal
    quote_size: Decimal
    maker: str
    last_points_block: int
    accum_points: Decimal


class OrderBook:
    def __init__(self) -> None:
        self._orders: Dict[str, _Order] = {}  # live orders
        self._totals: Dict[str, Dict[Decimal, Decimal]] = { # price-level totals
            "BUY": defaultdict(Decimal),
            "SELL": defaultdict(Decimal),
        }
        self._buy_heap: List[Decimal] = []  # store –price for max-heap
        self._sell_heap: List[Decimal] = []

    def _touch_price(self, side: str, price: Decimal) -> None:
        heapq.heappush(
            self._buy_heap if side == "BUY" 
                else self._sell_heap, 
            -price if side == "BUY" 
                else price
        )

    def _clean_heap(self, side: str) -> None:
        heap = self._buy_heap if side == "BUY" else self._sell_heap
        while heap:
            price = -heap[0] if side == "BUY" else heap[0]
            if price in self._totals[side]:
                return
            heapq.heappop(heap)

    def _best_price(self, side: str) -> Optional[Decimal]:
        self._clean_heap(side)
        heap = self._buy_heap if side == "BUY" else self._sell_heap
        if not heap:
            return None
        return -heap[0] if side == "BUY" else heap[0]

    def get_order(self, order_id: str) -> Optional[_Order]:
        return self._orders.get(order_id)

    def mid_price(self) -> Decimal:
        bid = self._best_price("BUY")
        ask = self._best_price("SELL")
        match (bid, ask):
            case (None, None):
                return Decimal(0)
            case (bid, None):
                return bid
            case (None, ask):
                return ask
            case (bid, ask):
                return (bid + ask) / 2

    def _accrue_order(self, order: _Order, mid: Decimal, blk: int) -> None:
        elapsed_blocks = blk - order.last_points_block
        if elapsed_blocks <= 0:
            return
        seconds = Decimal(elapsed_blocks) * BLOCK_SECONDS
        rate = _points_rate(order.quote_size, order.price, mid)
        order.accum_points += rate * seconds
        order.last_points_block = blk

    def accrue_points(self, mid: Decimal, blk: int) -> None:
        for order in self._orders.values():
            self._accrue_order(order, mid, blk)

    def accrue_points_for_order(self, order_id: str, mid: Decimal, blk: int) -> None:
        order = self._orders.get(order_id)
        if order is not None:
            self._accrue_order(order, mid, blk)

    def place_order(
        self,
        order_id: str,
        maker: str,
        side: str,
        price: Decimal,
        size: Decimal,
        blk: int,
    ) -> None:
        if order_id in self._orders:
            self.cancel_order(order_id)  # drop previous copy if for whatever reason it exist

        self._orders[order_id] = _Order(
            side=side,
            price=price,
            quote_size=size,
            maker=maker,
            last_points_block=blk,
            accum_points=Decimal(0),
        )
        self._totals[side][price] += size
        self._touch_price(side, price)

    def cancel_order(self, order_id: str) -> Optional[_Order]:
        order = self._orders.pop(order_id, None)
        if order is None:
            return None
        totals = self._totals[order.side]
        remaining = totals[order.price] - order.quote_size
        if remaining <= 0:
            totals.pop(order.price, None)
        else:
            totals[order.price] = remaining
        return order

    def update_order_size(
        self,
        order_id: str,
        price: Decimal,
        new_size: Decimal,
    ) -> None:
        if order_id not in self._orders:
            return # nonexistant order e.g. placed pre-season

        order = self._orders[order_id]
        assert order.price == price, "price mismatch on fill"

        delta = new_size - order.quote_size
        if delta == 0:
            return

        totals = self._totals[order.side]
        totals[price] += delta
        if totals[price] <= 0:
            totals.pop(price, None)

        order.quote_size = new_size
        self._touch_price(order.side, price)


class State:
    def __init__(self) -> None:
        self._books: Dict[str, OrderBook] = {}
        self._last_mid: Dict[str, Decimal] = {}  # market → last mid
        self.final_points: Dict[str, Decimal] = defaultdict(Decimal)  # maker → pts

    def _book(self, market: str) -> OrderBook:
        return self._books.setdefault(market, OrderBook())

    def _finalise_order(self, order: _Order) -> None:
        self.final_points[order.maker] += order.accum_points

    def mid_price(self, market: str) -> Decimal:
        return self._book(market).mid_price()

    def apply_orders_updated(self, ev: models.OrdersUpdated, market: str) -> None:
        book = self._book(market)

        for act in ev.actions:
            if act.action == "PLACE":
                book.place_order(
                    order_id=act.order_id,
                    maker=ev.caller,
                    side=act.side,
                    price=act.price,
                    size=act.quote_size,
                    blk=ev.block_number,
                )

            elif act.action == "CANCEL":
                mid = book.mid_price()
                book.accrue_points_for_order(act.order_id, mid, ev.block_number)
                order = book.cancel_order(act.order_id)
                if order is not None:
                    self._finalise_order(order)

        new_mid = book.mid_price()
        last_mid = self._last_mid.get(market)
        if last_mid is None or new_mid != last_mid:
            book.accrue_points(new_mid, ev.block_number)
            self._last_mid[market] = new_mid

    def apply_order_filled(self, ev: models.OrderFilled, market: str) -> None:
        book = self._book(market)
        mid = book.mid_price()

        for f in ev.fills:
            book.accrue_points_for_order(f.order_id, mid, ev.block_number)

            if f.new_quote_size == 0:
                order = book.cancel_order(f.order_id)
                if order is not None:
                    self._finalise_order(order)
            else:
                book.update_order_size(f.order_id, f.price, f.new_quote_size)

    def leaderboard(self) -> List[Tuple[str, Decimal]]:
        totals: Dict[str, Decimal] = defaultdict(Decimal)

        for maker, pts in self.final_points.items():
            totals[maker] += pts

        for ob in self._books.values():
            for order in ob._orders.values():
                totals[order.maker] += order.accum_points

        return sorted(totals.items(), key=lambda x: x[1], reverse=True)
