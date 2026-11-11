import random

from clinic_api.models import Patient

_rng = random.Random(7)


def make_patient(**overrides):
    n = _rng.randint(1000, 9999)
    values = {"id": n, "display_name": f"Patient {n}", "email": f"patient{n}@example.com"}
    values.update(overrides)
    return Patient(**values)
