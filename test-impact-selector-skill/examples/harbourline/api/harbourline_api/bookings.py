from .pricing import party_fare_cents

MAX_PARTY = 9


class BookingError(ValueError):
    pass


def create_booking(origin, destination, ages, hour):
    if not ages:
        raise BookingError("a booking needs at least one passenger")
    if len(ages) > MAX_PARTY:
        raise BookingError(f"at most {MAX_PARTY} passengers per booking")
    return {
        "route": f"{origin}:{destination}",
        "passengers": len(ages),
        "hour": hour,
        "total_cents": party_fare_cents(origin, destination, ages, hour),
    }
