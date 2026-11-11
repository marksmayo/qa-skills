import importlib

ENABLED = ("loyalty",)


def load(name):
    return importlib.import_module(f"harbourline_api.plugins.{name}")


def apply_all(booking, customer):
    for name in ENABLED:
        booking = load(name).apply(booking, customer)
    return booking
