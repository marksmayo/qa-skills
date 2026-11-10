"""Shopping cart."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Line:
    sku: str
    unit_price: Decimal
    quantity: int = 1


@dataclass
class Cart:
    lines: list[Line] = field(default_factory=list)

    def add(self, sku: str, unit_price: Decimal, quantity: int = 1) -> None:
        if quantity < 1:
            raise ValueError('quantity must be at least 1')
        self.lines.append(Line(sku, unit_price, quantity))

    def subtotal(self) -> Decimal:
        return sum((line.unit_price * line.quantity for line in self.lines), Decimal('0.00'))

    def total(self) -> Decimal:
        return self.subtotal()

    def apply_code(self, code: str, today: date) -> None:
        """Apply a promotion code. BILL-142 stub: signature only, no behaviour yet."""
        return None
