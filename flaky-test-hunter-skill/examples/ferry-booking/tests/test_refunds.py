from ferry_booking.refunds import credit, refund


def test_one_third_refund_rounds_to_nearest_cent():
    assert refund(10.00, 1 / 3) == 3.33


def test_full_credit():
    assert credit(9.15) == 9.15
