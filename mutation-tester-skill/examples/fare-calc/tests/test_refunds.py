from fare_calc.refunds import refund_cents


def test_full_refund_from_24_hours():
    assert refund_cents(999, 24) == 999


def test_half_refund_below_24_hours():
    assert refund_cents(999, 23) == 499


def test_half_refund_down_to_2_hours():
    assert refund_cents(999, 2) == 499


def test_no_refund_under_2_hours():
    assert refund_cents(999, 1) == 0
