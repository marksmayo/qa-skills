import re
from pathlib import Path

from .models import Patient

SEED = Path(__file__).resolve().parent.parent / "db" / "seed.sql"
ROW = re.compile(r"\((\d+), '([^']*)'\)")


def load_seed(path=SEED):
    return {int(pid): Patient(id=int(pid), display_name=name) for pid, name in ROW.findall(path.read_text(encoding="utf-8"))}


_PATIENTS = load_seed()


def get_patient(patient_id):
    return _PATIENTS[patient_id]
