from ledger.entries import balance, post_entry


def test_refund_reduces_the_balance():
    journal = []
    post_entry(journal, "sales", 5000)
    post_entry(journal, "sales", -1500)
    assert balance(journal, "sales") == 3500
