from harbourline_api.pricing import fare_cents, party_fare_cents, passenger_rate


def test_adult_pays_full_rate():
    assert passenger_rate(30) == 1.0


def test_child_pays_half():
    assert passenger_rate(8) == 0.5


def test_senior_pays_half():
    assert passenger_rate(70) == 0.5


def test_off_peak_adult_fare():
    assert fare_cents("circular-quay", "manly", 30, 11) == 980


def test_peak_fare_adds_thirty_percent():
    assert fare_cents("circular-quay", "manly", 30, 8) == 1274


def test_party_fare_sums_passengers():
    assert party_fare_cents("circular-quay", "watsons-bay", [30, 8], 12) == 760 + 380
