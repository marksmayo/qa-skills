def test_gold_customer_gets_ten_percent_off(client):
    body = {"origin": "barangaroo", "destination": "parramatta", "ages": [30], "hour": 12, "customer": {"trips": 52}}
    status, booking = client("/bookings", body)
    assert status == 201
    assert booking["loyalty"] == "gold"
    assert booking["total_cents"] == 1116


def test_new_customer_pays_full_fare(client):
    body = {"origin": "barangaroo", "destination": "parramatta", "ages": [30], "hour": 12, "customer": {"trips": 3}}
    _, booking = client("/bookings", body)
    assert "loyalty" not in booking
    assert booking["total_cents"] == 1240
