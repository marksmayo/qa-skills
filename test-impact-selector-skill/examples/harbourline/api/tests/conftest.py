import pytest

from harbourline_api.app import handle


@pytest.fixture
def client():
    def post(path, body):
        return handle({"path": path, "body": body})
    return post


@pytest.fixture
def family():
    return {"origin": "circular-quay", "destination": "manly", "ages": [41, 39, 12], "hour": 11}
