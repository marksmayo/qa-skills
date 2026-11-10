"""Fare lookup with an in-process cache."""

BASE_FARES = {"MANLY": 9.15, "TARONGA": 7.40, "PARRAMATTA": 11.20}
_CACHE: dict[str, float] = {}


def fare(route: str, concession: bool = False) -> float:
    """Adult fare for a route, half price for concessions. Results are cached per route."""
    key = route.upper()
    if key not in _CACHE:
        if key not in BASE_FARES:
            raise KeyError(f"unknown route {route!r}")
        _CACHE[key] = BASE_FARES[key]
    price = _CACHE[key]
    return round(price / 2, 2) if concession else price


def cached_routes() -> list[str]:
    return sorted(_CACHE)
