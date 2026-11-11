def test_concession_starts_at_65():
    assert fare_cents(1000, 64) == 1000
    assert fare_cents(1000, 65) == 500
