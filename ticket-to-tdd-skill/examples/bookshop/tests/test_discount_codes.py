"""BILL-142: customers can apply a discount code at checkout.

Red tests written from the ticket's acceptance criteria; they pass once the feature is built.
"""
from datetime import date
from decimal import Decimal

import pytest

from bookshop.promotions import DiscountError, find_code

TODAY = date(2026, 11, 10)


def test_percentage_code_reduces_total(cart_of):
    """BILL-142 REQ-1: a valid percentage code reduces the total by that percentage."""
    cart = cart_of('60.00', '40.00')
    cart.apply_code('SAVE10', today=TODAY)
    assert cart.total() == Decimal('90.00')


def test_code_applies_when_subtotal_equals_minimum_spend(cart_of):
    """BILL-142 REQ-1: "at least the minimum spend" includes a subtotal exactly equal to it."""
    cart = cart_of('50.00')
    cart.apply_code('SAVE20', today=TODAY)
    assert cart.total() == Decimal('40.00')


def test_discounted_total_is_rounded_to_the_nearest_cent(cart_of):
    """BILL-142 REQ-2: 19.99 less 10% is 17.991, shown as 17.99."""
    cart = cart_of('19.99')
    cart.apply_code('SAVE10', today=TODAY)
    assert cart.total() == Decimal('17.99')


def test_code_lookup_ignores_case_and_surrounding_spaces():
    """BILL-142 REQ-3: codes are case-insensitive and ignore surrounding spaces."""
    assert find_code('  save10 ') == find_code('SAVE10')


def test_expired_code_is_rejected_and_total_unchanged(cart_of):
    """BILL-142 REQ-4: an expired code is rejected with "This code has expired"."""
    cart = cart_of('100.00')
    with pytest.raises(DiscountError, match='This code has expired'):
        cart.apply_code('SPRING24', today=TODAY)
    assert cart.total() == Decimal('100.00')


def test_second_code_replaces_the_first(cart_of):
    """BILL-142 REQ-5: only one code per cart; applying a second code replaces the first."""
    cart = cart_of('100.00')
    cart.apply_code('SAVE10', today=TODAY)
    cart.apply_code('SAVE20', today=TODAY)
    assert cart.total() == Decimal('80.00')


def test_code_below_minimum_spend_is_rejected_with_shortfall(cart_of):
    """BILL-142 REQ-6: below the minimum spend, the message says how much more to spend."""
    cart = cart_of('30.00')
    with pytest.raises(DiscountError, match=r'Spend \$20\.00 more to use this code'):
        cart.apply_code('SAVE20', today=TODAY)
    assert cart.total() == Decimal('30.00')
