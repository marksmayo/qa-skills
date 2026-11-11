from .routes import route

CHILD_AGE = 16
CONCESSION_AGE = 65
PEAK_HOURS = range(7, 10)


def passenger_rate(age):
    if age < CHILD_AGE:
        return 0.5
    if age >= CONCESSION_AGE:
        return 0.5
    return 1.0


def fare_cents(origin, destination, age, hour):
    base = route(origin, destination)["base_cents"]
    if hour in PEAK_HOURS:
        base = base * 13 // 10
    return round(base * passenger_rate(age))


def party_fare_cents(origin, destination, ages, hour):
    return sum(fare_cents(origin, destination, age, hour) for age in ages)
