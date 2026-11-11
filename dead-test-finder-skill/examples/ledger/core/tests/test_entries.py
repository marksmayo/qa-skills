import pytest

from ledger.entries import LedgerError, balance, post_entry


def test_post_entry_records_a_sale():
    journal = []
    post_entry(journal, "sales", 12000, "invoice 1041")


def test_balance_sums_the_account():
    journal = []
    post_entry(journal, "sales", 12000)
    post_entry(journal, "sales", -2000)
    post_entry(journal, "bank", 10000)
    assert balance(journal, "sales") == 10000


def test_zero_entry_is_rejected():
    with pytest.raises(LedgerError):
        post_entry([], "sales", 0)
