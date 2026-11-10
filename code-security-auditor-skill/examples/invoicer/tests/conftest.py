import pytest

from invoicer import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICER_DB", str(tmp_path / "test.db"))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def login(client):
    def _login(email, password):
        return client.post("/login", data={"email": email, "password": password})

    return _login
