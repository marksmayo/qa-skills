from unittest import mock

from ledger import tax


def test_gst_on_one_hundred_dollars():
    price = 10000
    assert tax.gst(price) == 1500


def test_gst_on_a_hundred_dollar_invoice():
    invoice_total = 10000
    assert tax.gst(invoice_total) == 1500


def test_with_gst_adds_the_tax():
    assert tax.with_gst(2000) == 2300


def test_tax_is_calculated():
    with mock.patch("ledger.tax.calculate_tax", return_value=1500) as calc:
        assert tax.calculate_tax(10000, 0.15) == 1500
        calc.assert_called_once_with(10000, 0.15)
