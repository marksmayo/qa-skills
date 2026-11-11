from clinic_api.billing import format_fee


def test_fee_is_shown_in_dollars():
    assert format_fee(123450) == "$1,234.50"
