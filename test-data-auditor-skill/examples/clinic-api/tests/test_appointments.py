from clinic_api.appointments import from_record, total_minutes


def test_appointments_load(appointments):
    loaded = [from_record(a) for a in appointments]
    assert [a.kind.value for a in loaded] == ["assessment", "follow_up"]


def test_total_minutes(appointments):
    assert total_minutes(from_record(a) for a in appointments) == 75
