import csv
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def test_referral_export_has_every_row():
    with (FIXTURES / "referral_export.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 24


def test_room_report_is_an_html_table():
    assert "<table>" in (FIXTURES / "room_report.xls").read_text(encoding="utf-8")
