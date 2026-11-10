def test_list_requires_login(client):
    assert client.get("/invoices").status_code == 401


def test_list_shows_own_invoices(client, login):
    login("alice@example.test", "alice-test-pw")
    numbers = [inv["number"] for inv in client.get("/invoices").get_json()]
    assert numbers == ["INV-1001", "INV-1002"]
