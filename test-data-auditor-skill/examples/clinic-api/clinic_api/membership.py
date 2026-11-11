from datetime import date

from .models import Membership


def from_record(record):
    return Membership(patient_id=record["patient_id"], plan=record["plan"],
                      expires=date.fromisoformat(record["expires"]), visits_left=record.get("visits_left", 0))


def is_active(membership):
    return membership.expires >= date.today() and membership.visits_left > 0
