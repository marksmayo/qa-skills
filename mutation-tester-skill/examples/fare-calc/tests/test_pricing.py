from fare_calc.pricing import fare_cents, total


def test_adult_pays_full_fare():
    assert fare_cents(1000, 30) == 1000


def test_senior_pays_half_fare():
    assert fare_cents(1000, 70) == 500


def test_total_for_a_family():
    result = total([fare_cents(1000, 30), fare_cents(1000, 70)])
    print("family total", result)
    assert result
