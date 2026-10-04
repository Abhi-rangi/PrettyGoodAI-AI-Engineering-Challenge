"""The clinic system the receptionist reads from and writes to.

`ClinicBackend` is the seam to the practice-management app. The bot only ever talks to
this interface; `InMemoryBackend` implements it for local development and tests. The
production adapter (HTTP calls into the clinic app) is a separate class with the same
methods and is the one piece of integration work left.

Two rules every implementation must keep, because the voice layer relies on them:
  - `book` and `reschedule` re-check the slot and raise `SlotUnavailable` if it has gone.
    The bot only ever offers slots that came from `find_slots`, but someone else can take
    one between offering it and booking it.
  - Every method that takes a `patient_id` acts only on that patient's data.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Literal, Protocol
from zoneinfo import ZoneInfo

VisitType = Literal["evaluation", "follow_up"]
TimeOfDay = Literal["morning", "afternoon", "evening", "any"]

VISIT_MINUTES: dict[str, int] = {"evaluation": 60, "follow_up": 45}


class SlotUnavailable(Exception):
    """The slot was taken, or never existed, by the time we tried to book it."""


class NotFound(Exception):
    """No such appointment for this patient."""


@dataclass(frozen=True)
class NewPatient:
    first_name: str
    last_name: str
    date_of_birth: date
    phone: str  # 10 digits, normalised


@dataclass(frozen=True)
class Slot:
    id: str
    start: datetime  # timezone-aware, clinic local time
    minutes: int
    visit_type: str


@dataclass(frozen=True)
class Appointment:
    id: str
    patient_id: str
    start: datetime
    minutes: int
    visit_type: str


@dataclass(frozen=True)
class CallbackRequest:
    phone: str
    reason: str
    patient_id: str | None


class ClinicBackend(Protocol):
    async def find_patient(self, last_name: str, phone: str) -> str | None: ...
    async def find_patient_by_identity(self, first_name: str, last_name: str,
                                       date_of_birth: date) -> str | None: ...
    async def create_patient(self, patient: NewPatient) -> str: ...
    async def find_slots(self, visit_type: str, start: date, end: date,
                         time_of_day: str, limit: int) -> list[Slot]: ...
    async def book(self, patient_id: str, slot_id: str) -> Appointment: ...
    async def upcoming_appointments(self, patient_id: str) -> list[Appointment]: ...
    async def reschedule(self, patient_id: str, appointment_id: str,
                         slot_id: str) -> Appointment: ...
    async def cancel(self, patient_id: str, appointment_id: str) -> None: ...
    async def request_callback(self, request: CallbackRequest) -> None: ...


# ---------------------------------------------------------------- normalisation

def phone_digits(raw: str) -> str | None:
    """Ten-digit US number, or None. Accepts a leading 1 and any punctuation.

    The clinic app stores phones as typed, e.g. "(555) 123-4567", so every comparison
    happens on digits only, on both sides.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def name_key(raw: str) -> str:
    return re.sub(r"[^a-z]", "", (raw or "").lower())


# ------------------------------------------------------------------- in-memory

@dataclass
class _Patient:
    id: str
    first_name: str
    last_name: str
    date_of_birth: date
    phone: str


class InMemoryBackend:
    """One therapist, weekday hours, 30-minute grid. Enough to exercise every path."""

    def __init__(self, timezone: str = "America/New_York",
                 open_at: time = time(8, 0), close_at: time = time(18, 0),
                 now: Callable[[], datetime] | None = None):
        self.tz = ZoneInfo(timezone)
        self.open_at, self.close_at = open_at, close_at
        self._now = now or (lambda: datetime.now(self.tz))
        self.patients: dict[str, _Patient] = {}
        self.appointments: dict[str, Appointment] = {}
        self.callbacks: list[CallbackRequest] = []
        self._seq = 0

    @classmethod
    def seeded(cls, timezone: str = "America/New_York") -> "InMemoryBackend":
        backend = cls(timezone)
        for first, last, dob, phone in [
            ("Dana", "Whitfield", date(1992, 3, 12), "6095550142"),
            ("Marcus", "Bell", date(1979, 8, 3), "6095550188"),
        ]:
            backend._add_patient(first, last, dob, phone)
        return backend

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:05d}"

    def _add_patient(self, first: str, last: str, dob: date, phone: str) -> str:
        pid = self._next_id("P")
        self.patients[pid] = _Patient(pid, first, last, dob, phone)
        return pid

    async def find_patient(self, last_name: str, phone: str) -> str | None:
        key = name_key(last_name)
        for p in self.patients.values():
            if name_key(p.last_name) == key and phone_digits(p.phone) == phone:
                return p.id
        return None

    async def find_patient_by_identity(self, first_name, last_name, date_of_birth):
        for p in self.patients.values():
            if (name_key(p.first_name) == name_key(first_name)
                    and name_key(p.last_name) == name_key(last_name)
                    and p.date_of_birth == date_of_birth):
                return p.id
        return None

    async def create_patient(self, patient: NewPatient) -> str:
        return self._add_patient(patient.first_name, patient.last_name,
                                 patient.date_of_birth, patient.phone)

    def _busy(self, start: datetime, minutes: int, ignore: str | None = None) -> bool:
        end = start + timedelta(minutes=minutes)
        return any(
            a.start < end and start < a.start + timedelta(minutes=a.minutes)
            for a in self.appointments.values() if a.id != ignore
        )

    def _in_window(self, start: datetime, minutes: int, time_of_day: str) -> bool:
        local = start.astimezone(self.tz)
        end = local + timedelta(minutes=minutes)
        if local.weekday() >= 5 or local.time() < self.open_at or end.time() > self.close_at:
            return False
        if end.date() != local.date():
            return False
        hour = local.hour
        return {"morning": hour < 12, "afternoon": 12 <= hour < 17,
                "evening": hour >= 17}.get(time_of_day, True)

    async def find_slots(self, visit_type, start, end, time_of_day, limit):
        minutes = VISIT_MINUTES[visit_type]
        earliest = self._now() + timedelta(hours=2)
        found: list[Slot] = []
        day = start
        while day <= end and len(found) < limit:
            t = datetime.combine(day, self.open_at, self.tz)
            while t.date() == day and len(found) < limit:
                if (t >= earliest and self._in_window(t, minutes, time_of_day)
                        and not self._busy(t, minutes)):
                    found.append(Slot(f"{visit_type}@{t.isoformat()}", t, minutes, visit_type))
                t += timedelta(minutes=30)
            day += timedelta(days=1)
        return found

    def _parse_slot(self, slot_id: str) -> tuple[str, datetime]:
        try:
            visit_type, iso = slot_id.split("@", 1)
            start = datetime.fromisoformat(iso)
        except ValueError:
            raise SlotUnavailable(slot_id)
        if visit_type not in VISIT_MINUTES or start.tzinfo is None:
            raise SlotUnavailable(slot_id)
        return visit_type, start

    def _check_slot(self, slot_id: str, ignore: str | None = None) -> tuple[str, datetime, int]:
        visit_type, start = self._parse_slot(slot_id)
        minutes = VISIT_MINUTES[visit_type]
        if (start < self._now() or not self._in_window(start, minutes, "any")
                or self._busy(start, minutes, ignore)):
            raise SlotUnavailable(slot_id)
        return visit_type, start, minutes

    async def book(self, patient_id, slot_id):
        visit_type, start, minutes = self._check_slot(slot_id)
        appt = Appointment(self._next_id("A"), patient_id, start, minutes, visit_type)
        self.appointments[appt.id] = appt
        return appt

    async def upcoming_appointments(self, patient_id):
        now = self._now()
        return sorted((a for a in self.appointments.values()
                       if a.patient_id == patient_id and a.start >= now),
                      key=lambda a: a.start)

    def _owned(self, patient_id: str, appointment_id: str) -> Appointment:
        appt = self.appointments.get(appointment_id)
        if appt is None or appt.patient_id != patient_id:
            raise NotFound(appointment_id)
        return appt

    async def reschedule(self, patient_id, appointment_id, slot_id):
        old = self._owned(patient_id, appointment_id)
        visit_type, start, minutes = self._check_slot(slot_id, ignore=old.id)
        new = Appointment(old.id, patient_id, start, minutes, visit_type)
        self.appointments[old.id] = new
        return new

    async def cancel(self, patient_id, appointment_id):
        self._owned(patient_id, appointment_id)
        del self.appointments[appointment_id]

    async def request_callback(self, request):
        self.callbacks.append(request)
