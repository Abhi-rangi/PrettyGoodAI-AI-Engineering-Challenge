"""The receptionist's tools: schemas the model sees, and the handlers that run them.

Validation lives here, not in the prompt. A voice model cannot reliably count the
digits it heard or check that a date exists, so every handler checks its input and
returns a short error code the prompt knows how to recover from ("ask again").

The verified patient is held in `CallContext`, never passed by the model. Once
`lookup_patient` or `save_new_patient` succeeds, every later tool acts on that patient
and only that patient, so nothing the caller says can steer the bot onto someone
else's record.
"""
import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .backend import (VISIT_MINUTES, Appointment, CallbackRequest, ClinicBackend,
                      NewPatient, NotFound, Slot, SlotUnavailable, phone_digits)

TOOL_TIMEOUT_S = 8
MAX_LOOKUPS = 3
MAX_SEARCH_DAYS = 21
SLOTS_RETURNED = 6


class ToolError(Exception):
    def __init__(self, code: str, **detail: Any):
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass
class CallContext:
    caller_number: str | None  # 10 digits from caller ID, if Twilio supplied one
    timezone: str
    patient_id: str | None = None
    lookup_attempts: int = 0
    now: Callable[[], datetime] | None = None

    def today(self) -> date:
        tz = ZoneInfo(self.timezone)
        return (self.now() if self.now else datetime.now(tz)).astimezone(tz).date()


def spoken(dt: datetime, tz: str) -> str:
    """'Tuesday, October 14 at 9:30 AM'. Handed to the model so it never has to work
    out a weekday or convert a timestamp itself, which it gets wrong."""
    local = dt.astimezone(ZoneInfo(tz))
    hour = local.hour % 12 or 12
    minutes = "" if local.minute == 0 else f":{local.minute:02d}"
    return f"{local:%A, %B} {local.day} at {hour}{minutes} {'AM' if local.hour < 12 else 'PM'}"


def _parse_date(raw: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ToolError("invalid_date", field=field)


def _required_text(args: dict, field: str) -> str:
    value = str(args.get(field) or "").strip()
    if not value:
        raise ToolError("missing_field", field=field)
    return value


def _phone(args: dict, field: str = "phone") -> str:
    digits = phone_digits(str(args.get(field) or ""))
    if digits is None:
        raise ToolError("invalid_phone", field=field,
                        hint="need a 10-digit US number; ask the caller to repeat it")
    return digits


# --------------------------------------------------------------------- schemas

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


_STR = {"type": "string"}

TOOL_SCHEMAS: list[dict] = [
    _fn("lookup_patient",
        "Find an existing patient by last name and the phone number on their record.",
        {"last_name": _STR, "phone": {"type": "string", "description": "Digits as spoken."}},
        ["last_name", "phone"]),
    _fn("save_new_patient",
        "Create a record for a new patient after reading the details back to them.",
        {"first_name": _STR, "last_name": _STR,
         "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
         "phone": {"type": "string", "description": "Digits as spoken."}},
        ["first_name", "last_name", "date_of_birth", "phone"]),
    _fn("find_availability",
        "Search open appointment slots. Only ever offer slots this returns.",
        {"visit_type": {"type": "string", "enum": list(VISIT_MINUTES)},
         "earliest_date": {"type": "string", "description": "YYYY-MM-DD"},
         "latest_date": {"type": "string", "description": "YYYY-MM-DD"},
         "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening", "any"]}},
        ["visit_type", "earliest_date", "latest_date", "time_of_day"]),
    _fn("book_appointment",
        "Book a slot from find_availability for the identified patient.",
        {"slot_id": _STR}, ["slot_id"]),
    _fn("list_appointments",
        "List the identified patient's upcoming appointments.", {}, []),
    _fn("reschedule_appointment",
        "Move one of the patient's appointments to a slot from find_availability.",
        {"appointment_id": _STR, "new_slot_id": _STR}, ["appointment_id", "new_slot_id"]),
    _fn("cancel_appointment",
        "Cancel one of the patient's upcoming appointments.",
        {"appointment_id": _STR}, ["appointment_id"]),
    _fn("request_callback",
        "Ask the front desk to call the caller back.",
        {"phone": {"type": "string", "description": "Digits as spoken."},
         "reason": {"type": "string", "description": "One short sentence."}},
        ["phone", "reason"]),
    _fn("end_call",
        "Hang up. Call this right after you have said goodbye out loud.",
        {"reason": {"type": "string", "description": "One short sentence."}}, ["reason"]),
]


# -------------------------------------------------------------------- handlers

class ToolRouter:
    def __init__(self, backend: ClinicBackend, ctx: CallContext):
        self.backend, self.ctx = backend, ctx
        self._handlers: dict[str, Callable[[dict], Awaitable[dict]]] = {
            "lookup_patient": self.lookup_patient,
            "save_new_patient": self.save_new_patient,
            "find_availability": self.find_availability,
            "book_appointment": self.book_appointment,
            "list_appointments": self.list_appointments,
            "reschedule_appointment": self.reschedule_appointment,
            "cancel_appointment": self.cancel_appointment,
            "request_callback": self.request_callback,
        }

    async def call(self, name: str, arguments: str) -> dict:
        """Run one tool. Always returns a JSON-serialisable dict, never raises."""
        handler = self._handlers.get(name)
        if handler is None:
            return {"ok": False, "error": "unknown_tool"}
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return {"ok": False, "error": "bad_arguments"}
        try:
            result = await asyncio.wait_for(handler(args), timeout=TOOL_TIMEOUT_S)
            return {"ok": True, **result}
        except ToolError as exc:
            return {"ok": False, "error": exc.code, **exc.detail}
        except Exception as exc:  # backend down, timeout: the prompt offers a callback
            print(f"  ! tool {name} failed: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": "system_unavailable"}

    def _patient(self) -> str:
        if not self.ctx.patient_id:
            raise ToolError("caller_not_identified")
        return self.ctx.patient_id

    def _appt(self, a: Appointment) -> dict:
        return {"appointment_id": a.id, "visit_type": a.visit_type,
                "when": spoken(a.start, self.ctx.timezone)}

    def _slot(self, s: Slot) -> dict:
        return {"slot_id": s.id, "when": spoken(s.start, self.ctx.timezone)}

    # identity

    async def lookup_patient(self, args: dict) -> dict:
        if self.ctx.lookup_attempts >= MAX_LOOKUPS:
            # Stops the line being used to probe who is a patient.
            raise ToolError("too_many_attempts")
        last_name, phone = _required_text(args, "last_name"), _phone(args)
        self.ctx.lookup_attempts += 1
        patient_id = await self.backend.find_patient(last_name, phone)
        if patient_id is None:
            # Never say which of the two failed to match.
            return {"found": False, "attempts_left": MAX_LOOKUPS - self.ctx.lookup_attempts}
        self.ctx.patient_id = patient_id
        # Deliberately no record details: the model has nothing to read back.
        return {"found": True}

    async def save_new_patient(self, args: dict) -> dict:
        first, last = _required_text(args, "first_name"), _required_text(args, "last_name")
        dob = _parse_date(args.get("date_of_birth"), "date_of_birth")
        today = self.ctx.today()
        if not date(1900, 1, 1) <= dob < today:
            raise ToolError("invalid_date", field="date_of_birth",
                            hint="must be a real date in the past")
        phone = _phone(args)
        if await self.backend.find_patient_by_identity(first, last, dob):
            # Not attached to this call: name plus birth date is not enough to hand the
            # caller someone's appointments. They go through lookup, or get a callback.
            return {"saved": False, "already_registered": True}
        self.ctx.patient_id = await self.backend.create_patient(
            NewPatient(first, last, dob, phone))
        return {"saved": True}

    # scheduling

    async def find_availability(self, args: dict) -> dict:
        visit_type = args.get("visit_type")
        if visit_type not in VISIT_MINUTES:
            raise ToolError("invalid_visit_type", allowed=list(VISIT_MINUTES))
        today = self.ctx.today()
        earliest = max(_parse_date(args.get("earliest_date"), "earliest_date"), today)
        latest = _parse_date(args.get("latest_date"), "latest_date")
        if latest < earliest:
            raise ToolError("invalid_date_range")
        latest = min(latest, earliest + timedelta(days=MAX_SEARCH_DAYS))
        time_of_day = args.get("time_of_day") or "any"
        if time_of_day not in {"morning", "afternoon", "evening", "any"}:
            time_of_day = "any"
        slots = await self.backend.find_slots(visit_type, earliest, latest,
                                              time_of_day, SLOTS_RETURNED)
        return {"slots": [self._slot(s) for s in slots],
                "searched": f"{earliest.isoformat()} to {latest.isoformat()}"}

    async def book_appointment(self, args: dict) -> dict:
        patient_id = self._patient()
        try:
            appt = await self.backend.book(patient_id, _required_text(args, "slot_id"))
        except SlotUnavailable:
            raise ToolError("slot_taken", hint="apologise once and offer other slots")
        return {"booked": True, **self._appt(appt)}

    async def list_appointments(self, args: dict) -> dict:
        appts = await self.backend.upcoming_appointments(self._patient())
        return {"appointments": [self._appt(a) for a in appts]}

    async def reschedule_appointment(self, args: dict) -> dict:
        patient_id = self._patient()
        try:
            appt = await self.backend.reschedule(
                patient_id, _required_text(args, "appointment_id"),
                _required_text(args, "new_slot_id"))
        except NotFound:
            raise ToolError("appointment_not_found")
        except SlotUnavailable:
            raise ToolError("slot_taken", hint="apologise once and offer other slots")
        return {"rescheduled": True, **self._appt(appt)}

    async def cancel_appointment(self, args: dict) -> dict:
        try:
            await self.backend.cancel(self._patient(), _required_text(args, "appointment_id"))
        except NotFound:
            raise ToolError("appointment_not_found")
        return {"cancelled": True}

    async def request_callback(self, args: dict) -> dict:
        phone, reason = _phone(args), _required_text(args, "reason")
        await self.backend.request_callback(
            CallbackRequest(phone, reason[:300], self.ctx.patient_id))
        return {"requested": True}
