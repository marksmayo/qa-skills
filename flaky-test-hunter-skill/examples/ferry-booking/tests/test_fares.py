from ferry_booking.fares import cached_routes, fare


def test_cache_starts_empty():
    assert cached_routes() == []


def test_concession_is_half_price():
    assert fare("parramatta", concession=True) == 5.60


def test_unknown_route_is_rejected():
    try:
        fare("ATLANTIS")
    except KeyError:
        return
    raise AssertionError("expected KeyError")
