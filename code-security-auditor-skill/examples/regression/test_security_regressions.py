import io
from types import SimpleNamespace

import pytest

from invoicer import create_app
from invoicer.db import get_db

ALICE = ("alice@example.test", "alice-test-pw")


def test_app_refuses_to_start_without_secret_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("INVOICER_DB", str(tmp_path / "t.db"))
    with pytest.raises(RuntimeError):
        create_app()


def test_report_title_is_not_evaluated_as_a_template(client, login):
    login(*ALICE)
    body = client.get("/invoices/report", query_string={"title": "{{7*191}}"}).get_data(as_text=True)
    assert "1337" not in body


def test_search_cannot_widen_the_query(client, login):
    login(*ALICE)
    numbers = [i["number"] for i in client.get("/invoices/search", query_string={"q": "' OR 1=1 --"}).get_json()]
    assert "INV-2001" not in numbers


def test_cannot_read_another_users_invoice(client, login):
    login(*ALICE)
    assert client.get("/invoices/3").status_code == 404


def test_attachment_name_cannot_leave_uploads(client, login):
    login(*ALICE)
    assert client.get("/attachments", query_string={"name": "../requirements.txt"}).status_code == 404


def test_webhook_test_refuses_loopback_urls(client, login, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "invoicer.webhooks.requests.get",
        lambda url, **kw: calls.append(url) or SimpleNamespace(status_code=200, text="ok"),
    )
    login(*ALICE)
    resp = client.post("/webhooks/test", json={"url": "http://127.0.0.1/healthz"})
    assert resp.status_code == 400 and calls == []


def test_sort_accepts_only_known_columns(client, login):
    login(*ALICE)
    resp = client.get("/invoices", query_string={"sort": "CASE WHEN (1=1) THEN amount ELSE number END"})
    assert resp.status_code == 400


def test_login_ignores_an_external_next_url(client):
    resp = client.post("/login?next=https://canary.example/", data={"email": ALICE[0], "password": ALICE[1]})
    assert resp.headers["Location"].startswith("/")


def test_import_rejects_python_yaml_tags(client, login):
    login(*ALICE)
    doc = b"- number: !!python/object/apply:builtins.str [INV-9001]\n  customer: Canary\n  amount: 1\n"
    resp = client.post("/invoices/import", data={"file": (io.BytesIO(doc), "x.yaml")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_reset_token_is_long_enough(client):
    client.post("/password-reset", data={"email": ALICE[0]})
    with client.application.app_context():
        token = get_db().execute("SELECT reset_token FROM users WHERE id = 1").fetchone()[0]
    assert len(token) >= 32


def test_login_does_not_log_the_password(client, caplog):
    caplog.set_level("INFO")
    client.post("/login", data={"email": ALICE[0], "password": ALICE[1]})
    assert ALICE[1] not in caplog.text
