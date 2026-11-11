from fare_calc.manifest import manifest_lines


def test_manifest_numbers_passengers_in_order():
    assert manifest_lines(["Ana", "Ben", "Cy"]) == ["1. Ana", "2. Ben", "3. Cy"]


def test_empty_manifest():
    assert manifest_lines([]) == []
