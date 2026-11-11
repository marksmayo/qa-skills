from tests.factories import make_patient


def test_factory_patients_use_reserved_addresses():
    assert make_patient().email.endswith("@example.com")
