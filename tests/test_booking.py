from datetime import timedelta

from conftest import call, geocode_result, say, talk

from voice_agent.agent.services.maps import Location

DETAILS = {
    "customer_name": "Jane Doe",
    "phone": "+15555550123",
    "address": "12 Oak St",
    "city": "Denver",
    "zip_code": "80202",
    "issue": "No heat",
}


def search_from_tomorrow(first_window):
    return call("find_available_slots", earliest_day=str(first_window.start.date()))


def last_tool_result(model) -> str:
    return model.received[-1][-1].content


async def test_search_then_book_creates_one_event_and_one_row(make_session, calendar, storage, first_window):
    session, model = make_session(
        say("Where is it?"),
        search_from_tomorrow(first_window),
        say("I have tomorrow morning. Does that work?"),
        call("book_service_call", slot_id=first_window.id),
        say("You're booked."),
        details=[DETAILS],
    )

    await talk(session, "My furnace stopped")
    assert await talk(session, "Can someone call me tomorrow?") == "I have tomorrow morning. Does that work?"
    assert first_window.id in last_tool_result(model)
    assert await talk(session, "Yes, that's all correct.") == "You're booked."

    assert last_tool_result(model).startswith("Booked: ") and "(service call SC-" in last_tool_result(model)
    assert len(calendar.events) == 1
    [row] = storage.service_calls()
    assert row["call_id"] == "CA-test" and row["status"] == "booked" and row["customer_name"] == "Jane Doe"
    assert row["event_id"] == calendar.events[0]["id"] and row["start"] == first_window.start.isoformat()


async def test_slot_that_was_not_offered_is_rejected(make_session, calendar, storage, first_window):
    session, model = make_session(
        say("Which address is it?"),
        call("book_service_call", slot_id=first_window.id),
        say("Sorry."),
        details=[DETAILS],
    )

    await talk(session, "Book me tomorrow at 8")
    await talk(session, "Book me tomorrow at 8")

    assert "not in the latest search" in last_tool_result(model)
    assert calendar.events == [] and storage.service_calls() == []


async def test_second_booking_on_the_same_call_returns_the_first(make_session, calendar, first_window):
    session, model = make_session(
        say("Sure, what time?"),
        search_from_tomorrow(first_window),
        call("book_service_call", slot_id=first_window.id),
        call("book_service_call", slot_id=first_window.id),
        say("Done."),
        details=[DETAILS],
    )

    await talk(session, "Here are my details")
    await talk(session, "Book the first window tomorrow")

    assert last_tool_result(model).startswith("Already booked on this call")
    assert len(calendar.events) == 1


async def test_window_taken_after_search_is_not_booked(make_session, calendar, storage, first_window):
    session, model = make_session(
        say("Where is it?"),
        search_from_tomorrow(first_window),
        say("Tomorrow morning is open."),
        call("book_service_call", slot_id=first_window.id),
        say("That one was just taken."),
        details=[DETAILS],
    )
    await talk(session, "My furnace stopped")
    await talk(session, "Tomorrow please")

    # Both technicians get booked by other callers before this caller confirms.
    for calendar_id in ("ana@calendar", "ben@calendar"):
        calendar.busy_times[calendar_id] = [(first_window.start, first_window.end)]
    await talk(session, "Yes")

    assert "just taken" in last_tool_result(model)
    assert calendar.events == [] and storage.service_calls() == []


async def test_unreadable_calendars_are_never_offered_as_free(make_session, calendar, first_window):
    calendar.unreadable = {"ana@calendar", "ben@calendar"}
    session, model = make_session(
        say("Where is it?"), search_from_tomorrow(first_window), say("Our calendar is down."), details=[DETAILS]
    )

    await talk(session, "My furnace stopped")
    await talk(session, "Tomorrow please")

    assert "can't be reached" in last_tool_result(model)


async def test_search_skips_windows_where_every_technician_is_busy(make_session, calendar, settings, first_window):
    for calendar_id in ("ana@calendar", "ben@calendar"):
        calendar.busy_times[calendar_id] = [(first_window.start, first_window.end)]
    session, model = make_session(
        say("Where is it?"), search_from_tomorrow(first_window), say("Later that day works."), details=[DETAILS]
    )

    await talk(session, "My furnace stopped")
    await talk(session, "Tomorrow please")

    result = last_tool_result(model)
    assert first_window.id not in result
    assert (first_window.start + timedelta(minutes=settings.service_call_minutes)).strftime("%Y-%m-%d %H:%M") in result


async def test_call_reported_as_urgent_is_not_booked(make_session, calendar, storage, first_window):
    storage.add_urgent_request({"id": "UR-1", "call_id": "CA-test", "issue": "Water pouring from the furnace"})
    session, model = make_session(
        say("Sure."),
        search_from_tomorrow(first_window),
        call("book_service_call", slot_id=first_window.id),
        say("The team already has it."),
        details=[DETAILS],
    )

    await talk(session, "Here are my details")
    await talk(session, "Can you also book a call tomorrow?")

    assert "already reported as urgent" in last_tool_result(model)
    assert calendar.events == [] and storage.service_calls() == []


async def test_address_outside_the_service_radius_gets_no_times(make_session, storage, maps, first_window):
    storage.technicians_file.write_text("name,calendar_id,address\nAna,ana@calendar,Far Away\n", encoding="utf-8")
    maps.known["far away"] = Location(40.7128, -74.0060)  # New York, well beyond the 30-mile radius of Denver
    session, model = make_session(
        say("Where are you?"), search_from_tomorrow(first_window), say("Sorry."), details=[DETAILS]
    )

    await talk(session, "My furnace is dead")
    await talk(session, "Tomorrow please")

    assert "outside our service area" in last_tool_result(model)


async def test_technicians_in_range_are_offered(make_session, storage, maps, first_window):
    storage.technicians_file.write_text("name,calendar_id,address\nAna,ana@calendar,Nearby\n", encoding="utf-8")
    maps.known["nearby"] = Location(39.75, -105.0)  # a mile or so from the customer
    session, model = make_session(
        say("Where are you?"), search_from_tomorrow(first_window), say("Here."), details=[DETAILS]
    )

    await talk(session, "My furnace is dead")
    await talk(session, "Tomorrow please")

    assert first_window.id in last_tool_result(model)


async def test_times_are_not_searched_before_every_detail_is_known(make_session, first_window):
    session, model = make_session(
        say("Sure, where is it?"),
        search_from_tomorrow(first_window),
        say("And your name?"),
        details=[{"issue": "No heat", "address": "12 Oak St", "city": "Denver", "zip_code": "80202"}],
    )

    await talk(session, "My furnace stopped, 12 Oak St, Denver")
    await talk(session, "Can someone call me tomorrow?")

    assert "still missing customer name, phone" in last_tool_result(model)


async def test_search_only_offers_times_in_the_wanted_part_of_the_day(make_session, first_window):
    afternoon = first_window.start.replace(hour=14, minute=0)
    session, model = make_session(
        say("Where is it?"),
        call("find_available_slots", earliest_day=str(afternoon.date()), earliest_time="14:00", latest_time="16:00"),
        say("Here are two."),
        details=[DETAILS],
    )

    await talk(session, "My furnace stopped")
    await talk(session, "Any afternoon works")

    # A line reads "2026-09-17 14:15: Thursday, September 17, at 2:15 PM", so the slot_id ends at the first ": ".
    offered = [line.split(": ", 1)[0] for line in last_tool_result(model).splitlines() if line.startswith("20")]
    assert offered and all("14:00" <= slot[-5:] <= "16:00" for slot in offered)


async def test_search_uses_the_validated_address_city_and_zip_without_another_lookup(make_session, maps, first_window):
    response = geocode_result("19 Spring Street", "New York", "10012")
    maps.responses["19 Spring Street, New York, 10012, USA"] = [response]
    session, model = make_session(
        say("When works?"),
        call("find_available_slots", earliest_day=str(first_window.start.date())),
        say("Here are the times."),
        details=[{**DETAILS, "address": "19 Spring Street", "city": "New York", "zip_code": "10012"}],
    )
    await talk(session, "Here are my details")
    await talk(session, "Tomorrow please")
    assert "Address used: 19 Spring Street, New York, 10012." in last_tool_result(model)
    assert maps.requests == ["19 Spring Street, New York, 10012, USA"]


async def test_failed_search_clears_previously_offered_times(make_session, first_window):
    session, _ = make_session(
        search_from_tomorrow(first_window),
        say("Here are the times."),
        call("find_available_slots", earliest_day="invalid"),
        say("Which day?"),
        details=[DETAILS],
    )
    await talk(session, "Here are my details")
    before = (await session.graph.aget_state(session.config)).values
    assert before["offered_slots"]
    await talk(session, "Another day")
    after = (await session.graph.aget_state(session.config)).values
    assert after["offered_slots"] == []
