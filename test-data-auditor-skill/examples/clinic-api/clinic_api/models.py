from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class AppointmentKind(Enum):
    ASSESSMENT = "assessment"
    FOLLOW_UP = "follow_up"
    HYDRO = "hydro"


@dataclass
class Patient:
    id: int
    display_name: str
    email: str = ""
    phone: str = ""
    medicare_number: str = ""
    address: str = ""
    dob: str = ""


@dataclass
class Appointment:
    id: int
    patient_id: int
    kind: AppointmentKind
    starts_at: str
    minutes: int = 30
    notes: str = ""


@dataclass
class Membership:
    patient_id: int
    plan: str
    expires: date
    visits_left: int = field(default=0)
