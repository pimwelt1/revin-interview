from datetime import timedelta

from conftest import call, say, talk

CALLER = "+15555550123"  # the number make_session's caller calls from


def add_booked_call(storage, calendar, window, calendar_id="ana@calendar", **overrides):
    """A call booked on an earlier phone call."""
    event_id = calendar.create_event(calendar_id, window.start, window.end, "Call Jane", "")
    row = {
        "id": "SC-OLD",
        "call_id": "CA-earlier",
        "status": "booked",
        "customer_name": "Jane Doe",
        "phone": "1 (555) 555-0123",
        "calling_from": "",
        "address": "12 Oak St",
        "issue": "No heat",
        "technician": "Ana" if calendar_id == "ana@calendar" else "Ben",
        "calendar_id": calendar_id,
        "event_id": event_id,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        **overrides,
    }
    storage.add_service_call(row)
    return row


def later(window, steps=2):
    return window.start + timedelta(minutes=15 * steps)


def slot_id(moment):
    return moment.strftime("%Y-%m-%d %H:%M")


def last_tool_result(model) -> str:
    return model.received[-1][-1].content


async def test_lookup_finds_only_the_callers_upcoming_booked_calls(make_session, storage, calendar, first_window):
    add_booked_call(storage, calendar, first_window)  # phone formatted differently, same number
    add_booked_call(storage, calendar, first_window, id="SC-OTHER", phone="+15555559999")
    add_booked_call(storage, calendar, first_window, id="SC-CANCELLED", status="cancelled")
    session, model = make_session(call("find_my_service_calls"), say("I see your call."))

    await talk(session, "I need to change my call")

    found = last_tool_result(model)
    assert "SC-OLD" in found and "SC-OTHER" not in found and "SC-CANCELLED" not in found


async def test_reschedule_keeps_the_technician_and_moves_the_event(make_session, storage, calendar, first_window):
    booked = add_booked_call(storage, calendar, first_window)
    new_start = later(first_window)
    session, model = make_session(
        call("find_my_service_calls"),
        call("find_available_slots", earliest_day=str(first_window.start.date())),
        call("reschedule_service_call", service_call_id="SC-OLD", slot_id=slot_id(new_start)),
        say("Moved."),
    )

    await talk(session, "Move my call a bit later please")

    assert last_tool_result(model).startswith("Rescheduled: Ana will now call")
    [row] = storage.service_calls()
    assert row["start"] == new_start.isoformat() and row["event_id"] == booked["event_id"]
    assert calendar.event(booked["event_id"])["start"] == new_start


async def test_reschedule_moves_to_another_technician_when_the_first_is_busy(
    make_session, storage, calendar, first_window
):
    booked = add_booked_call(storage, calendar, first_window)
    new_start = later(first_window)
    session, model = make_session(
        call("find_my_service_calls"),
        call("find_available_slots", earliest_day=str(first_window.start.date())),
        call("reschedule_service_call", service_call_id="SC-OLD", slot_id=slot_id(new_start)),
        say("Moved."),
    )
    calendar.busy_times["ana@calendar"].append((new_start, new_start + timedelta(minutes=15)))

    await talk(session, "Move my call a bit later please")

    assert last_tool_result(model).startswith("Rescheduled: Ben will now call")
    [row] = storage.service_calls()
    assert row["calendar_id"] == "ben@calendar" and row["event_id"] != booked["event_id"]
    assert [event["calendar_id"] for event in calendar.events] == ["ben@calendar"]  # old event deleted


async def test_changes_require_a_call_found_for_this_caller(make_session, storage, calendar, first_window):
    add_booked_call(storage, calendar, first_window, phone="+15555559999")  # someone else's call
    session, model = make_session(call("cancel_service_call", service_call_id="SC-OLD"), say("I can't find it."))

    await talk(session, "Cancel SC-OLD")

    assert "isn't in the latest find_my_service_calls" in last_tool_result(model)
    assert storage.service_calls()[0]["status"] == "booked" and len(calendar.events) == 1


async def test_cancel_deletes_the_event_and_marks_the_row(make_session, storage, calendar, first_window):
    add_booked_call(storage, calendar, first_window)
    session, model = make_session(
        call("find_my_service_calls"), call("cancel_service_call", service_call_id="SC-OLD"), say("Cancelled.")
    )

    await talk(session, "Please cancel my call")

    assert last_tool_result(model).startswith("Cancelled:")
    assert storage.service_calls()[0]["status"] == "cancelled" and calendar.events == []


def test_new_column_upgrades_an_existing_csv(storage):
    storage.service_calls_file.write_text("id,status,phone\nSC-1,booked,5555550123\n", encoding="utf-8")

    storage.add_service_call({"id": "SC-2", "status": "booked", "phone": "5555550123", "calling_from": CALLER})

    rows = storage.service_calls()
    assert [row["id"] for row in rows] == ["SC-1", "SC-2"] and rows[1]["calling_from"] == CALLER
    assert [row["id"] for row in storage.service_calls_for_phone(CALLER)] == ["SC-1", "SC-2"]
