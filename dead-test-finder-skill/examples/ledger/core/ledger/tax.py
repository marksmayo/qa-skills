GST_RATE = 0.15


def calculate_tax(amount_cents, rate):
    return round(amount_cents * rate)


def gst(amount_cents):
    return calculate_tax(amount_cents, GST_RATE)


def with_gst(amount_cents):
    return amount_cents + gst(amount_cents)
