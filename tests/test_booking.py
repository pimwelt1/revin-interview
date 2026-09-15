from datetime import timedelta

from conftest import call, say, talk

DETAILS = {"customer_name": "Jane Doe", "phone": "+15555550123", "address": "12 Oak St, Denver", "issue": "No heat"}


def search_from_tomorrow(first_window):
    return call("find_available_slots", earliest_day=str(first_window.start.date()))


def last_tool_result(model) -> str:
    return model.received[-1][-1].content


async def test_search_then_book_creates_one_event_and_one_row(make_session, calendar, storage, first_window):
    session, model = make_session(
        search_from_tomorrow(first_window),
        say("I have tomorrow morning. Does that work?"),
        call("book_service_call", slot_id=first_window.id, **DETAILS),
        say("You're booked."),
    )

    assert await talk(session, "My furnace stopped, can someone come tomorrow?") == "I have tomorrow morning. Does that work?"
    assert first_window.id in last_tool_result(model)
    assert await talk(session, "Yes, that's all correct.") == "You're booked."

    assert last_tool_result(model).startswith("Booked: ") and "(service call SC-" in last_tool_result(model)
    assert len(calendar.events) == 1
    [row] = storage.service_calls()
    assert row["call_id"] == "CA-test" and row["status"] == "booked" and row["customer_name"] == "Jane Doe"
    assert row["event_id"] == calendar.events[0]["id"] and row["start"] == first_window.start.isoformat()


async def test_slot_that_was_not_offered_is_rejected(make_session, calendar, storage, first_window):
    session, model = make_session(call("book_service_call", slot_id=first_window.id, **DETAILS), say("Sorry."))

    await talk(session, "Book me tomorrow at 8")

    assert "not in the latest search" in last_tool_result(model)
    assert calendar.events == [] and storage.service_calls() == []


async def test_second_booking_on_the_same_call_returns_the_first(make_session, calendar, first_window):
    session, model = make_session(
        search_from_tomorrow(first_window),
        call("book_service_call", slot_id=first_window.id, **DETAILS),
        call("book_service_call", slot_id=first_window.id, **DETAILS),
        say("Done."),
    )

    await talk(session, "Book the first window tomorrow")

    assert last_tool_result(model).startswith("Already booked on this call")
    assert len(calendar.events) == 1


async def test_window_taken_after_search_is_not_booked(make_session, calendar, storage, first_window):
    session, model = make_session(
        search_from_tomorrow(first_window),
        say("Tomorrow morning is open."),
        call("book_service_call", slot_id=first_window.id, **DETAILS),
        say("That one was just taken."),
    )
    await talk(session, "Tomorrow please")

    # Both technicians get booked by other callers before this caller confirms.
    for calendar_id in ("ana@calendar", "ben@calendar"):
        calendar.busy_times[calendar_id] = [(first_window.start, first_window.end)]
    await talk(session, "Yes")

    assert "just taken" in last_tool_result(model)
    assert calendar.events == [] and storage.service_calls() == []


async def test_unreadable_calendars_are_never_offered_as_free(make_session, calendar, first_window):
    calendar.unreadable = {"ana@calendar", "ben@calendar"}
    session, model = make_session(search_from_tomorrow(first_window), say("Our calendar is down."))

    await talk(session, "Tomorrow please")

    assert "can't be reached" in last_tool_result(model)


async def test_search_skips_windows_where_every_technician_is_busy(make_session, calendar, settings, first_window):
    for calendar_id in ("ana@calendar", "ben@calendar"):
        calendar.busy_times[calendar_id] = [(first_window.start, first_window.end)]
    session, model = make_session(search_from_tomorrow(first_window), say("Later that day works."))

    await talk(session, "Tomorrow please")

    result = last_tool_result(model)
    assert first_window.id not in result
    assert (first_window.start + timedelta(minutes=settings.service_call_minutes)).strftime("%Y-%m-%d %H:%M") in result


async def test_call_reported_as_urgent_is_not_booked(make_session, calendar, storage, first_window):
    storage.add_urgent_request({"id": "UR-1", "call_id": "CA-test", "issue": "Water pouring from the furnace"})
    session, model = make_session(
        search_from_tomorrow(first_window),
        call("book_service_call", slot_id=first_window.id, **DETAILS),
        say("The team already has it."),
    )

    await talk(session, "Can you also book a call tomorrow?")

    assert "already reported as urgent" in last_tool_result(model)
    assert calendar.events == [] and storage.service_calls() == []
