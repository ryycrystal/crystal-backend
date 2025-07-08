from __future__ import annotations

"""in‑memory state management for obs

the class is not thread‑safe; callers should ensure they interact from the same asyncio
thread (which the rest of the codebase already does)
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import heapq
from typing import Dict, List, Tuple

import helpers as h
import models


@dataclass
class _Order:
    side: str
    price: Decimal
    quote_size: Decimal


class OrderBook:
    def __init__(self) -> None:
        self._orders: Dict[str, _Order] = {} # single source of truth for every live order
        self._totals: Dict[str, Dict[Decimal, Decimal]] = { # price-aggregate dict
            "BUY": defaultdict(Decimal),
            "SELL": defaultdict(Decimal),
        }
        # heaps for best prices – duplicated prices are fine, we pop until valid
        self._buy_heap: List[Decimal] = []  # store negative prices for max‑heap b/c of native min-heap
        self._sell_heap: List[Decimal] = []

    def _touch_price(self, side: str, price: Decimal) -> None:
        if side == "BUY":
            heapq.heappush(self._buy_heap, -price)
        else:
            heapq.heappush(self._sell_heap, price)

    def _clean_heap(self, side: str) -> None: # lazily clean
        heap = self._buy_heap if side == "BUY" else self._sell_heap
        while heap:
            price = -heap[0] if side == "BUY" else heap[0]
            if price in self._totals[side]:
                return
            heapq.heappop(heap)

    def _best_price(self, side: str) -> Decimal | None:
        self._clean_heap(side)
        heap = self._buy_heap if side == "BUY" else self._sell_heap
        if not heap:
            return None
        return -heap[0] if side == "BUY" else heap[0]

    def place_order(self, order_id: str, side: str, price: Decimal, size: Decimal) -> None:
        if order_id in self._orders: # if for whatever reason it's a duplicate log we override old one
            self.cancel_order(order_id)
        self._orders[order_id] = _Order(side, price, size)
        self._totals[side][price] += size
        self._touch_price(side, price)

    def cancel_order(self, order_id: str) -> None:
        order = self._orders.pop(order_id, None)
        if order is None:
            return
        totals = self._totals[order.side]
        remaining = totals[order.price] - order.quote_size
        if remaining <= 0:
            totals.pop(order.price, None)
        else:
            totals[order.price] = remaining

    def update_order_size(self, order_id: str, price: Decimal, new_size: Decimal) -> None:
        if order_id not in self._orders:
            # if the exchange never saw the place (e.g., fill happened in same blk)
            # treat as no‑op
            return
        order = self._orders[order_id]
        assert order.price == price, "price mismatch on fill"
        delta = new_size - order.quote_size
        if delta == 0:
            return
        totals = self._totals[order.side]
        totals[price] += delta
        if totals[price] <= 0:
            totals.pop(price, None)
        if new_size == 0:
            self._orders.pop(order_id, None)
        else:
            order.quote_size = new_size
        self._touch_price(order.side, price)

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


class State:
    def __init__(self) -> None:
        self._books: Dict[str, OrderBook] = {}

    def _book(self, market: str) -> OrderBook:
        return self._books.setdefault(market, OrderBook())

    def apply_orders_updated(self, ev: models.OrdersUpdated, market: str) -> None:
        book = self._book(market)
        for act in ev.actions:
            if act.action == "PLACE":
                book.place_order(act.order_id, act.side, act.price, act.quote_size)
            elif act.action == "CANCEL":
                book.cancel_order(act.order_id)

    def apply_order_filled(self, ev: models.OrderFilled, market: str) -> None:
        book = self._book(market)
        for f in ev.fills:
            book.update_order_size(f.order_id, f.price, f.new_quote_size)

    def mid_price(self, market: str):
        return self._book(market).mid_price()
