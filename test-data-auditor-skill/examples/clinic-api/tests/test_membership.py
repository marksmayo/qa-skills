from clinic_api.membership import from_record, is_active


def test_ten_pack_member_is_active(memberships):
    assert is_active(from_record(memberships[0]))


def test_expired_casual_member_is_not_active(memberships):
    assert not is_active(from_record(memberships[1]))
