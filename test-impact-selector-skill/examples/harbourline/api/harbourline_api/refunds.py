FULL_REFUND_HOURS = 24


def refund_cents(total_cents, hours_before_departure):
    if hours_before_departure >= FULL_REFUND_HOURS:
        return total_cents
    if hours_before_departure >= 2:
        return total_cents // 2
    return 0
