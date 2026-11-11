def test_operator_cancellation_refunds_everything_plus_goodwill():
    assert refund_cents(999, 1, cancelled_by_operator=True) == 1499
