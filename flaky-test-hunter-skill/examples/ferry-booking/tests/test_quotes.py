from ferry_booking.fares import fare


def test_manly_quote():
    assert fare("manly") == 9.15


def test_quote_is_case_insensitive():
    assert fare("Taronga") == fare("TARONGA")
