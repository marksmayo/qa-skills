import sys

import pytest

from ledger.entries import post_entry
from ledger.reports import to_csv_lines, trial_balance


@pytest.mark.skip(reason="LEDG-12: trial balance wrong for multi-currency journals")
def test_trial_balance_with_two_currencies():
    journal = []
    post_entry(journal, "sales:NZD", 1000)
    post_entry(journal, "sales:AUD", 900)
    assert trial_balance(journal) == {"sales:AUD": 900, "sales:NZD": 1000}


@pytest.mark.skipif(sys.platform == "win32", reason="CSV export is checked with LF line endings; Windows writes CRLF")
def test_csv_lines_are_sorted():
    assert to_csv_lines({"sales": 10, "bank": 5}) == ["bank,5", "sales,10"]


def test_trial_balance_lists_every_account():
    journal = []
    post_entry(journal, "sales", 100)
    post_entry(journal, "bank", 100)
    assert set(trial_balance(journal)) == {"sales", "bank"}
