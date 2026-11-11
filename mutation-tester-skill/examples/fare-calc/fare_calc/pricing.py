"""Fare pricing."""
import logging

log = logging.getLogger(__name__)

BOOKING_FEE_CENTS = 150
CONCESSION_AGE = 65


def concession_rate(age: int) -> float:
    """Passengers aged 65 and over travel at half fare."""
    if age >= CONCESSION_AGE:
        return 0.5
    return 1.0


def fare_cents(base_cents: int, age: int) -> int:
    return round(base_cents * concession_rate(age))


def total(fares: list) -> int:
    """Sum of the fares plus one booking fee per booking."""
    subtotal = sum(fares)
    log.info("priced %d fares", len(fares))
    return subtotal + BOOKING_FEE_CENTS
