GOLD_TRIPS = 40


def apply(booking, customer):
    if customer.get("trips", 0) >= GOLD_TRIPS:
        booking = {**booking, "total_cents": booking["total_cents"] * 9 // 10, "loyalty": "gold"}
    return booking
