"""Promotion codes, maintained by the promo team."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class PromoCode:
    code: str
    percent_off: int
    min_spend: Decimal
    expires_on: date


CODES = {
    'SAVE10': PromoCode('SAVE10', 10, Decimal('0.00'), date(2030, 12, 31)),
    'SAVE20': PromoCode('SAVE20', 20, Decimal('50.00'), date(2030, 12, 31)),
    'SPRING24': PromoCode('SPRING24', 15, Decimal('0.00'), date(2024, 5, 31)),
}


class DiscountError(Exception):
    """A promotion code was rejected. BILL-142 stub: declared so the new tests can import it."""


def find_code(code: str) -> PromoCode | None:
    """Look up a promotion code, ignoring surrounding spaces and case."""
    return CODES.get(code.strip().upper())
