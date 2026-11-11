from harbourline_api.app import handle


def test_refund_quote_on_the_day():
    status, body = handle({"path": "/refunds/quote", "body": {"total_cents": 1500, "hours_before": 3}})
    assert status == 200
    assert body == {"refund_cents": 750}


def test_unknown_path_is_404():
    status, _ = handle({"path": "/nope", "body": {}})
    assert status == 404
