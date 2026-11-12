import importlib

from clinic_api import settings


def test_default_currency():
    assert settings.CURRENCY == "AUD"


def test_currency_can_be_overridden(monkeypatch):
    monkeypatch.setenv("CLINIC_CURRENCY", "NZD")
    importlib.reload(settings)
    assert settings.CURRENCY == "NZD"
