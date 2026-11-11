import json
from pathlib import Path

from clinic_api.insurer import InsurerClient

CASSETTE = Path(__file__).parent / "cassettes" / "insurer_cover.json"


def replay(method, path):
    recorded = json.loads(CASSETTE.read_text(encoding="utf-8"))
    assert (method, path) == (recorded["request"]["method"], recorded["request"]["path"])
    return recorded["response"]


def test_cover_is_read_from_the_insurer():
    assert InsurerClient(replay).cover("M-1001") == "extras-gold"
