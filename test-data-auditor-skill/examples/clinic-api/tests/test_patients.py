from clinic_api.models import Patient
from clinic_api.store import get_patient


def test_seed_patient_is_loaded():
    assert get_patient(42).display_name == "Seed Patient B"


def test_patient_fixture_builds_models(patients):
    built = [Patient(**p) for p in patients]
    assert [p.id for p in built] == [1, 2]
    assert built[0].display_name == "Harriet Quinlan"
