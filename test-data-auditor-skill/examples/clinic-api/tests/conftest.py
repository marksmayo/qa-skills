import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_yaml_list(path):
    """Read the small YAML fixtures used here: a list of flat mappings."""
    records, current = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("- "):
            current = {}
            records.append(current)
            line = "  " + line[2:]
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"')
        current[key] = int(value) if value.isdigit() else value
    return records


@pytest.fixture
def patients():
    return json.loads((FIXTURES / "patients.json").read_text(encoding="utf-8"))


@pytest.fixture
def memberships():
    return json.loads((FIXTURES / "memberships.json").read_text(encoding="utf-8"))


@pytest.fixture
def appointments():
    return load_yaml_list(FIXTURES / "appointments.yaml")


@pytest.fixture(scope="session")
def waitlist():
    return []
