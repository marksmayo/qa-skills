import time

import pytest


def basket_total(lines, code=None):
    total = sum(price * qty for price, qty in lines)
    if code == "SAVE10":
        total = round(total * 0.9, 2)
    return total


@pytest.mark.checkout
def test_total_sums_lines():
    assert basket_total([(12.50, 2), (3.00, 1)]) == 28.00


@pytest.mark.checkout
def test_discount_code_takes_ten_percent():
    assert basket_total([(40.00, 1)], code="SAVE10") == 36.00


@pytest.mark.checkout
def test_discount_code_is_case_insensitive():
    # planted failure: the code is compared case-sensitively
    assert basket_total([(40.00, 1)], code="save10") == 36.00


@pytest.mark.checkout
@pytest.mark.slow
def test_large_basket_total():
    time.sleep(1.2)
    assert basket_total([(1.00, 1)] * 500) == 500.00


@pytest.mark.payments
def test_card_payment_is_authorised():
    assert "authorised" == "authorised"
