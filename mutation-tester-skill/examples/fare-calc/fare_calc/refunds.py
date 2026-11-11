"""Refunds for cancelled sailings."""
import logging

log = logging.getLogger(__name__)

GOODWILL_CENTS = 500


def refund_cents(paid_cents: int, hours_before: int, cancelled_by_operator: bool = False) -> int:
    """Full refund 24 hours or more before sailing, half from 2 hours, nothing after that.

    When the operator cancels, the passenger gets everything back plus a goodwill credit.
    """
    if cancelled_by_operator:
        credit = paid_cents + GOODWILL_CENTS
        log.warning("operator cancellation: refunding %d cents", credit)
        return credit
    if hours_before >= 24:
        return paid_cents
    if hours_before >= 2:
        return paid_cents // 2
    return 0
