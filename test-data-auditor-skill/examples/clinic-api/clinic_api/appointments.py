from dataclasses import fields

from .models import Appointment, AppointmentKind

_FIELDS = {f.name for f in fields(Appointment)}


def from_record(record):
    known = {k: v for k, v in record.items() if k in _FIELDS}
    known["kind"] = AppointmentKind(known["kind"])
    return Appointment(**known)


def total_minutes(appointments):
    return sum(a.minutes for a in appointments)
