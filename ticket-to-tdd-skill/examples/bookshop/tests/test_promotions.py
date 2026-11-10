from bookshop.promotions import find_code


def test_unknown_code_is_not_found():
    assert find_code('NOPE') is None


def test_known_code_is_found():
    assert find_code('SAVE10').percent_off == 10
