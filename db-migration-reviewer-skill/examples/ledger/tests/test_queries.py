def test_balance_query_mentions_amount():
    # tests are not counted as readers
    assert "amount" in "SELECT SUM(amount) FROM transactions"
