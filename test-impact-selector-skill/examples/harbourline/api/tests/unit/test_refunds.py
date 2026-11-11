from harbourline_api.refunds import refund_cents


def test_full_refund_a_day_ahead():
    assert refund_cents(2000, 30) == 2000


def test_half_refund_on_the_day():
    assert refund_cents(2000, 5) == 1000


def test_no_refund_at_the_gate():
    assert refund_cents(2000, 1) == 0
