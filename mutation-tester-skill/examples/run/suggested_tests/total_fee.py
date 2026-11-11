def test_total_adds_one_booking_fee():
    # 1000 (adult) + 500 (senior) + 150 booking fee
    assert total([fare_cents(1000, 30), fare_cents(1000, 70)]) == 1650
