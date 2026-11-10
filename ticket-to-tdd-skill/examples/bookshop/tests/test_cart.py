from decimal import Decimal

import pytest

from bookshop.cart import Cart


def test_subtotal_sums_lines(cart_of):
    assert cart_of('12.50', '7.50').subtotal() == Decimal('20.00')


def test_quantity_multiplies_unit_price():
    cart = Cart()
    cart.add('SKU-1', Decimal('4.25'), quantity=3)
    assert cart.subtotal() == Decimal('12.75')


def test_quantity_below_one_is_rejected():
    with pytest.raises(ValueError, match='at least 1'):
        Cart().add('SKU-1', Decimal('1.00'), quantity=0)


def test_total_without_code_equals_subtotal(cart_of):
    assert cart_of('10.00').total() == Decimal('10.00')
