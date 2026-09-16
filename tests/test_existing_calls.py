from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import DENVER, call, say, talk

from voice_agent.agent.details import accept_details
from voice_agent.agent.tools.existing_calls import reschedule_service_call
from voice_agent.agent.tools.scheduling import book_service_call, find_available_slots

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
        "city": "Denver",
        "zip_code": "80202",
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


@pytest.mark.parametrize("has_booking", [False, True])
async def test_lookup_excludes_urgent_email_requests(make_session, storage, calendar, first_window, has_booking):
    storage.add_urgent_request(
        {"id": "UR-EMAIL", "call_id": "CA-urgent", "phone": CALLER, "calling_from": CALLER, "issue": "Burning smell"}
    )
    if has_booking:
        add_booked_call(storage, calendar, first_window)
    session, model = make_session(call("find_my_service_calls"), say("I've checked your booked calls."))

    await talk(session, "What calls do I have booked?")

    found = last_tool_result(model)
    assert "UR-EMAIL" not in found and "Burning smell" not in found
    if has_booking:
        assert "SC-OLD" in found
    else:
        assert "No upcoming technician calls found" in found


async def test_reschedule_keeps_the_technician_and_moves_the_event(make_session, storage, calendar, first_window):
    booked = add_booked_call(storage, calendar, first_window)
    new_start = later(first_window)
    session, model = make_session(
        call("find_my_service_calls"),
        say("I see your call tomorrow. Move it to later?"),
        call("find_available_slots", earliest_day=str(first_window.start.date()), service_call_id="SC-OLD"),
        call("reschedule_service_call", service_call_id="SC-OLD", slot_id=slot_id(new_start)),
        say("Moved."),
    )

    await talk(session, "I need to move my call")
    await talk(session, "Yes, a bit later please")

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
        say("I see your call tomorrow. Move it to later?"),
        call("find_available_slots", earliest_day=str(first_window.start.date()), service_call_id="SC-OLD"),
        call("reschedule_service_call", service_call_id="SC-OLD", slot_id=slot_id(new_start)),
        say("Moved."),
    )
    calendar.busy_times["ana@calendar"].append((new_start, new_start + timedelta(minutes=15)))

    await talk(session, "I need to move my call")
    await talk(session, "Yes, a bit later please")

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
        call("find_my_service_calls"),
        say("Your call is tomorrow at 8. Cancel it?"),
        call("cancel_service_call", service_call_id="SC-OLD"),
        say("Cancelled."),
    )

    await talk(session, "Please cancel my call")
    await talk(session, "Yes, cancel it")

    assert last_tool_result(model).startswith("Cancelled:")
    assert storage.service_calls()[0]["status"] == "cancelled" and calendar.events == []


def test_new_column_upgrades_an_existing_csv(storage):
    storage.service_calls_file.write_text("id,status,phone\nSC-1,booked,5555550123\n", encoding="utf-8")

    storage.add_service_call({"id": "SC-2", "status": "booked", "phone": "5555550123", "calling_from": CALLER})

    rows = storage.service_calls()
    assert [row["id"] for row in rows] == ["SC-1", "SC-2"] and rows[1]["calling_from"] == CALLER
    assert [row["id"] for row in storage.service_calls_for_phone(CALLER)] == ["SC-1", "SC-2"]


async def test_a_call_cannot_be_changed_on_the_turn_it_was_listed(make_session, storage, calendar, first_window):
    add_booked_call(storage, calendar, first_window)
    session, model = make_session(
        call("find_my_service_calls"), call("cancel_service_call", service_call_id="SC-OLD"), say("Shall I cancel it?")
    )

    await talk(session, "This is Jane Doe, cancel my call")

    assert "Read the call back" in last_tool_result(model)
    assert storage.service_calls()[0]["status"] == "booked" and len(calendar.events) == 1


@pytest.mark.parametrize("details", [{}, {"customer_name": "Sam Ortiz"}, {"customer_name": "Jane"}])
async def test_cancellation_uses_phone_lookup_without_name_checks(
    make_session, storage, calendar, first_window, details
):
    add_booked_call(storage, calendar, first_window)
    session, model = make_session(
        call("find_my_service_calls"),
        say("Cancel this call?"),
        call("cancel_service_call", service_call_id="SC-OLD"),
        say("Cancelled."),
        details=[details],
    )
    await talk(session, "Cancel my call")
    await talk(session, "Yes")
    assert last_tool_result(model).startswith("Cancelled:")
    assert storage.service_calls()[0]["status"] == "cancelled"


async def test_lookup_does_not_populate_customer_facts_or_geocode(make_session, storage, calendar, first_window, maps):
    add_booked_call(storage, calendar, first_window)
    add_booked_call(
        storage, calendar, first_window, id="SC-TWO", address="161 W 54th St", city="New York", zip_code="10019"
    )
    session, _ = make_session(call("find_my_service_calls"), say("Which call?"))
    await talk(session, "I need to move one of my calls")
    state = (await session.graph.aget_state(session.config)).values
    assert state["facts"] == {} and state["pending_details"] == {}
    assert maps.requests == []
    assert set(state["found_service_calls"]) == {"SC-OLD", "SC-TWO"}


async def test_reschedule_legacy_booking_without_collecting_details(make_session, storage, calendar, first_window):
    booked = add_booked_call(storage, calendar, first_window, address="12 Oak St, Denver", city="", zip_code="")
    new_start = later(first_window)
    session, model = make_session(
        call("find_my_service_calls"),
        say("Move this call?"),
        call("find_available_slots", earliest_day=str(first_window.start.date()), service_call_id="SC-OLD"),
        call("reschedule_service_call", service_call_id="SC-OLD", slot_id=slot_id(new_start)),
        say("Moved."),
    )
    await talk(session, "Move my call")
    await talk(session, "Yes, later please")
    assert last_tool_result(model).startswith("Rescheduled:")
    row = storage.service_calls()[0]
    for field in ("customer_name", "phone", "address", "city", "zip_code", "issue"):
        assert row[field] == booked[field]
    assert row["start"] == new_start.isoformat()


def runtime_for(context, **state):
    return SimpleNamespace(
        context=context,
        state={"call_id": "CA-test", "turn_id": 2, "found_on_turn": 1, "found_service_calls": ["SC-OLD"], **state},
        tool_call_id="test",
    )


def test_replacement_search_uses_chosen_bookings_address_not_facts(context, first_window, monkeypatch):
    from voice_agent.agent.tools import scheduling

    add_booked_call(context.storage, context.calendar, first_window)
    add_booked_call(
        context.storage,
        context.calendar,
        first_window,
        id="SC-TWO",
        address="161 W 54th St",
        city="New York",
        zip_code="10019",
    )
    used = []

    def covering(ctx, address):
        used.append(address)
        return ctx.storage.technicians()

    monkeypatch.setattr(scheduling, "serving_technicians", covering)
    runtime = runtime_for(context, found_service_calls=["SC-OLD", "SC-TWO"], facts={"address": "unrelated"})
    answer = find_available_slots.func(runtime, earliest_day=str(first_window.start.date()), service_call_id="SC-TWO")
    assert used == ["161 W 54th St, New York, 10019"]
    assert answer.update["offered_slots"]
    assert answer.update["offered_for_service_call"] == "SC-TWO"
    assert "facts" not in answer.update


@pytest.mark.parametrize("identifier,status", [("SC-OTHER", "booked"), ("SC-OLD", "cancelled")])
def test_replacement_search_rejects_unknown_or_cancelled_booking(context, first_window, identifier, status):
    add_booked_call(context.storage, context.calendar, first_window, id=identifier, status=status)
    runtime = runtime_for(context, offered_slots=[first_window.id])
    answer = find_available_slots.func(runtime, service_call_id=identifier)
    assert answer.update["messages"][0].content.startswith("Not searched:")
    assert answer.update["offered_slots"] == []


def test_rescheduling_cannot_use_another_bookings_search(context, first_window):
    add_booked_call(context.storage, context.calendar, first_window)
    offered = slot_id(later(first_window))
    runtime = runtime_for(context, offered_slots=[offered], offered_for_service_call="SC-TWO")
    assert "search replacement times for this service_call_id" in reschedule_service_call.func(
        "SC-OLD", offered, runtime
    )
    assert context.storage.service_calls()[0]["start"] == first_window.start.isoformat()


def test_new_booking_cannot_use_replacement_search(context, first_window):
    details = {
        "customer_name": "Jane Doe",
        "phone": "5555550123",
        "address": "12 Oak St",
        "city": "Denver",
        "zip_code": "80202",
        "issue": "No heat",
    }
    state = accept_details({}, details, context.maps)
    runtime = runtime_for(context, **state)
    runtime.state.update(offered_slots=[first_window.id], offered_for_service_call="SC-OLD")
    assert "existing call" in book_service_call.func(first_window.id, runtime)
    assert context.calendar.events == []


def test_reschedule_preserves_customer_details_despite_conversation_corrections(context, first_window):
    booked = add_booked_call(context.storage, context.calendar, first_window)
    context.maps.known["12 oak st, denver, 80202"] = DENVER
    offered = slot_id(later(first_window))
    runtime = runtime_for(
        context,
        offered_slots=[offered],
        offered_for_service_call="SC-OLD",
        facts={"customer_name": "Someone Else", "phone": "5555550999", "address": "999 Other St"},
        pending_details={"zip_code": "bad"},
        detail_errors={"zip_code": "invalid ZIP"},
    )
    assert reschedule_service_call.func("SC-OLD", offered, runtime).startswith("Rescheduled:")
    row = context.storage.service_calls()[0]
    for field in ("customer_name", "phone", "address", "city", "zip_code", "issue"):
        assert row[field] == booked[field]
