from decimal import Decimal

import pytest

from bookshop.cart import Cart


@pytest.fixture
def cart_of():
    """Build a cart with one line per price, e.g. cart_of('12.50', '7.50')."""
    def make(*prices: str) -> Cart:
        cart = Cart()
        for i, price in enumerate(prices, 1):
            cart.add(f'SKU-{i}', Decimal(price))
        return cart
    return make
