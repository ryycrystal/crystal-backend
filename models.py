from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import List


@dataclass(slots=True)
class Fill:
    price: Decimal
    order_id: str
    new_quote_size: Decimal


@dataclass(slots=True)
class OrderFilled:
    block_number: int
    caller: str
    side: str
    amount_in: Decimal
    amount_out: Decimal
    start_price: Decimal
    end_price: Decimal
    fills: List[Fill]


@dataclass(slots=True)
class OrderAction:
    action: str
    side: str
    price: Decimal
    order_id: str
    quote_size: Decimal


@dataclass(slots=True)
class OrdersUpdated:
    block_number: int
    caller: str
    actions: List[OrderAction]