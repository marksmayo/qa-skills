"""Refunds for cancelled trips."""
from decimal import ROUND_HALF_UP, Decimal


def refund(paid: float, fraction: float) -> float:
    """The refund for a fraction of a paid fare, to the cent."""
    amount = Decimal(str(paid)) * Decimal(str(fraction))
    # Bug: always rounds up instead of to the nearest cent.
    return float(amount.quantize(Decimal("0.01"), rounding="ROUND_CEILING"))


def credit(paid: float) -> float:
    return float(Decimal(str(paid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
