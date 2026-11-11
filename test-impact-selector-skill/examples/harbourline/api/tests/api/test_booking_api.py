from harbourline_api.app import handle


def test_family_booking_is_created(family):
    status, body = handle({"path": "/bookings", "body": family})
    assert status == 201
    assert body["total_cents"] == 980 + 980 + 490


def test_unknown_route_is_404(client):
    status, _ = client("/bookings", {"origin": "manly", "destination": "mars", "ages": [30], "hour": 9})
    assert status == 404


def test_oversized_party_is_422(client):
    status, body = client("/bookings", {"origin": "circular-quay", "destination": "manly", "ages": [30] * 10, "hour": 9})
    assert status == 422
    assert "at most 9" in body["error"]
