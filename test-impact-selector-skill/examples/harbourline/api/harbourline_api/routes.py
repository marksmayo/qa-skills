import json
from pathlib import Path

_ROUTES = json.loads((Path(__file__).parent / "data" / "routes.json").read_text(encoding="utf-8"))


def route(origin, destination):
    key = f"{origin}:{destination}"
    if key not in _ROUTES:
        raise KeyError(f"no route {key}")
    return _ROUTES[key]
