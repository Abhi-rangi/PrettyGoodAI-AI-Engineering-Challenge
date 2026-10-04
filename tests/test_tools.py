import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from receptionist.backend import InMemoryBackend, phone_digits
from receptionist.tools import CallContext, ToolRouter

TZ = "America/New_York"
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo(TZ))  # a Monday


def make_router():
    backend = InMemoryBackend(TZ, now=lambda: NOW)
    backend._add_patient("Dana", "Whitfield", date(1992, 3, 12), "(609) 555-0142")
    ctx = CallContext(caller_number="6095550142", timezone=TZ, now=lambda: NOW)
    return ToolRouter(backend, ctx), backend


def call(router, name, **args):
    return asyncio.run(router.call(name, json.dumps(args)))


def test_phone_digits():
    assert phone_digits("+1 (609) 555-0142") == "6095550142"
    assert phone_digits("609 555 014") is None


def test_lookup_matches_on_digits_and_returns_no_details():
    router, _ = make_router()
    result = call(router, "lookup_patient", last_name="whitfield", phone="609-555-0142")
    assert result == {"ok": True, "found": True}
    assert router.ctx.patient_id is not None


def test_lookup_locks_out_after_three_misses():
    router, _ = make_router()
    for _ in range(3):
        assert call(router, "lookup_patient", last_name="Nobody",
                    phone="6095550000")["found"] is False
    result = call(router, "lookup_patient", last_name="Whitfield", phone="6095550142")
    assert result["error"] == "too_many_attempts"
    assert router.ctx.patient_id is None


@pytest.mark.parametrize("dob,phone,error", [
    ("2026-02-30", "6095550100", "invalid_date"),
    ("2030-01-01", "6095550100", "invalid_date"),
    ("1990-01-01", "555-0100", "invalid_phone"),
])
def test_save_new_patient_validates(dob, phone, error):
    router, _ = make_router()
    result = call(router, "save_new_patient", first_name="Ann", last_name="Lee",
                  date_of_birth=dob, phone=phone)
    assert result["error"] == error


def test_save_new_patient_does_not_attach_an_existing_record():
    router, _ = make_router()
    result = call(router, "save_new_patient", first_name="Dana", last_name="Whitfield",
                  date_of_birth="1992-03-12", phone="6095559999")
    assert result["already_registered"] is True
    assert router.ctx.patient_id is None


def test_booking_requires_an_identified_caller():
    router, _ = make_router()
    assert call(router, "book_appointment", slot_id="x")["error"] == "caller_not_identified"
    assert call(router, "list_appointments")["error"] == "caller_not_identified"


def test_book_list_reschedule_cancel_flow():
    router, _ = make_router()
    call(router, "save_new_patient", first_name="Ann", last_name="Lee",
         date_of_birth="1990-01-01", phone="6095550100")
    found = call(router, "find_availability", visit_type="evaluation",
                 earliest_date="2026-10-01", latest_date="2026-10-12", time_of_day="morning")
    slots = found["slots"]
    assert slots and found["searched"].startswith("2026-10-05")  # past dates clamped
    assert slots[0]["when"] == "Monday, October 5 at 11 AM"  # 2h lead time
    assert all("Saturday" not in s["when"] and "Sunday" not in s["when"] for s in slots)

    booked = call(router, "book_appointment", slot_id=slots[0]["slot_id"])
    assert booked["booked"] is True
    # The same slot is gone, and is no longer offered.
    assert call(router, "book_appointment", slot_id=slots[0]["slot_id"])["error"] == "slot_taken"
    again = call(router, "find_availability", visit_type="evaluation",
                 earliest_date="2026-10-05", latest_date="2026-10-05", time_of_day="morning")
    assert slots[0]["slot_id"] not in {s["slot_id"] for s in again["slots"]}

    appt_id = call(router, "list_appointments")["appointments"][0]["appointment_id"]
    moved = call(router, "reschedule_appointment", appointment_id=appt_id,
                 new_slot_id=slots[2]["slot_id"])
    assert moved["rescheduled"] is True and moved["when"] == slots[2]["when"]
    assert call(router, "cancel_appointment", appointment_id=appt_id)["cancelled"] is True
    assert call(router, "list_appointments")["appointments"] == []


def test_cannot_touch_another_patients_appointment():
    router, backend = make_router()
    other = backend._add_patient("Zed", "Other", date(1980, 1, 1), "6095550111")
    slot = asyncio.run(backend.find_slots("follow_up", NOW.date(), NOW.date(), "any", 1))[0]
    appt = asyncio.run(backend.book(other, slot.id))
    call(router, "lookup_patient", last_name="Whitfield", phone="6095550142")
    result = call(router, "cancel_appointment", appointment_id=appt.id)
    assert result["error"] == "appointment_not_found"


def test_invented_slot_is_rejected():
    router, _ = make_router()
    call(router, "lookup_patient", last_name="Whitfield", phone="6095550142")
    saturday = "follow_up@2026-10-10T10:00:00-04:00"
    assert call(router, "book_appointment", slot_id=saturday)["error"] == "slot_taken"
    assert call(router, "book_appointment", slot_id="garbage")["error"] == "slot_taken"


def test_bad_arguments_and_backend_failure_never_raise():
    router, backend = make_router()
    assert asyncio.run(router.call("lookup_patient", "{not json")) == \
        {"ok": False, "error": "bad_arguments"}

    async def boom(*_):
        raise ConnectionError("clinic app down")
    backend.find_patient = boom
    result = call(router, "lookup_patient", last_name="Whitfield", phone="6095550142")
    assert result == {"ok": False, "error": "system_unavailable"}


def test_callback_is_recorded():
    router, backend = make_router()
    assert call(router, "request_callback", phone="6095550142",
                reason="insurance question")["requested"] is True
    assert backend.callbacks[0].phone == "6095550142"
