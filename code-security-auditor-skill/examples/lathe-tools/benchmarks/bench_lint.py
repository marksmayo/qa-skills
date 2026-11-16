import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pkg-a' / 'scripts'))
import lint  # noqa: E402


def test_self_test_passes():
    assert lint.self_test() == 0
