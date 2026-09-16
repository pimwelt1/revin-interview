from copy import deepcopy
from types import SimpleNamespace

import pytest
from conftest import call, geocode_result, say, talk

from voice_agent.agent.details import (
    accept_details,
    describe,
    details_needed,
    full_address,
    normalize_phone,
    problems,
    read_details,
)
from voice_agent.agent.services.composio import ComposioError
from voice_agent.agent.services.storage import same_phone
from voice_agent.agent.tools.urgent import report_urgent_issue

COMPLETE = {
    "customer_name": "Jane Doe",
    "phone": "+1 555 555 0123",
    "address": "12 Oak St",
    "city": "Denver",
    "zip_code": "80202",
    "issue": "No heat",
}
QUERY = "12 Oak St, Denver, 80202, USA"


def test_extraction_preserves_incomplete_values_null_and_explicit_withdrawal():
    assert read_details('{"customer_name":" Jane ","phone":null,"city":""}') == {"customer_name": "Jane", "city": ""}
    with pytest.raises(ValueError):
        read_details('{"zip_code": 1234}')


@pytest.mark.parametrize("value", ["5555550123", "1 (555) 555-0123", "+1 555.555.0123"])
def test_phone_formats_share_one_normalization(value):
    assert normalize_phone(value) == "+15555550123"
    assert same_phone(value, "+15555550123")


@pytest.mark.parametrize(
    "value", ["5550123", "555555012", "25555550123", "555ABC0123", "+5555550123", "5555550123 ext 4"]
)
def test_invalid_phone_cannot_be_accepted_or_used_for_identity(value):
    assert not normalize_phone(value)
    assert not same_phone(value, value)


def test_invalid_fields_do_not_discard_valid_fields_or_mutate_input(maps):
    original = {"facts": {"issue": "No heat"}}
    snapshot = deepcopy(original)
    update = accept_details(original, {"customer_name": "Jane Doe", "phone": "555555012"}, maps)
    assert original == snapshot
    assert update["facts"] == {"issue": "No heat", "customer_name": "Jane Doe"}
    assert update["pending_details"]["phone"] == "555555012"
    assert "9 digits" in details_needed(update)


def test_partial_address_is_remembered_without_geocoding_or_accepting_it(maps):
    state = accept_details({}, {"address": "12 Oak St"}, maps)
    assert not state["facts"] and not maps.requests
    # The street is remembered, so it is absent from the list of what is still owed.
    assert "still missing customer name, phone, city, ZIP code, issue" in details_needed(state)
    state = accept_details(state, {"city": "Denver"}, maps)
    assert not state["facts"] and not maps.requests
    assert "still missing customer name, phone, ZIP code, issue" in details_needed(state)
    state = accept_details(state, {"zip_code": "80202"}, maps)
    assert full_address(state["facts"]) == "12 Oak St, Denver, 80202"
    assert not state["pending_details"] and not state["detail_errors"]
    assert maps.requests == [QUERY]


def test_name_completion_and_punctuation(maps):
    state = accept_details({}, {"customer_name": "Jane"}, maps)
    assert "customer_name" not in state["facts"]
    assert "last name" in describe(state)
    state = accept_details(state, {"customer_name": "Jane Doe"}, maps)
    assert state["facts"]["customer_name"] == "Jane Doe"
    assert not state["pending_details"] and not state["detail_errors"]
    assert problems({"customer_name": "José O’Neill-Santos"}) == {}


def test_invalid_corrections_block_old_accepted_details(maps):
    state = accept_details({}, COMPLETE, maps)
    state["offered_slots"] = ["2030-01-01 08:00"]
    state = accept_details(state, {"phone": "555", "zip_code": "12"}, maps)
    assert "phone" not in state["facts"] and "address" not in state["facts"]
    assert state["pending_details"]["address"] == "12 Oak St"
    assert state["pending_details"]["city"] == "Denver"
    assert state["offered_slots"] == []
    assert "phone" in state["detail_errors"] and "zip_code" in state["detail_errors"]
    assert len(maps.requests) == 1


def test_withdrawn_details_are_no_longer_usable(maps):
    state = accept_details({}, COMPLETE, maps)
    state = accept_details(state, {"phone": "", "city": ""}, maps)
    assert "phone" not in state["facts"] and "address" not in state["facts"]
    assert "still missing phone, city" in details_needed(state)


def test_maps_outage_keeps_address_pending_and_retries_without_repeat(maps):
    maps.responses[QUERY] = ComposioError("service down")
    state = accept_details({}, COMPLETE, maps)
    assert "address" not in state["facts"]
    assert "temporarily unavailable" in details_needed(state)
    maps.responses[QUERY] = [geocode_result()]
    state = accept_details(state, {}, maps)
    assert full_address(state["facts"]) == "12 Oak St, Denver, 80202"
    assert not state["detail_errors"]


def test_incomplete_street_and_zip_formats_never_reach_maps(maps):
    state = accept_details({}, {**COMPLETE, "address": "Oak Street 80202", "zip_code": "1234"}, maps)
    assert "street number" in state["detail_errors"]["address"]
    assert "five digits" in state["detail_errors"]["zip_code"]
    assert not maps.requests


async def test_validation_feedback_is_available_before_the_first_spoken_reply(make_session):
    session, model = make_session(say("Could you repeat your number?"), details=[{"phone": "555555012"}])
    spoken = await talk(session, "My number is 555555012")
    assert spoken == "Could you repeat your number?"
    assert "9 digits" in model.received[0][0].content
    state = (await session.graph.aget_state(session.config)).values
    assert "phone" not in state["facts"]
    assert state["pending_details"]["phone"] == "555555012"


async def test_partial_address_can_be_completed_across_caller_turns(make_session, maps):
    session, model = make_session(
        say("What city and ZIP code?"),
        say("What ZIP code?"),
        say("And your full name?"),
        details=[{"address": "12 Oak St"}, {"city": "Denver"}, {"zip_code": "80202"}],
    )
    await talk(session, "12 Oak St")
    await talk(session, "Denver")
    await talk(session, "80202")
    assert "still missing customer name, phone, ZIP code, issue" in model.received[1][0].content
    state = (await session.graph.aget_state(session.config)).values
    assert full_address(state["facts"]) == "12 Oak St, Denver, 80202"
    assert maps.requests == [QUERY]


async def test_invalid_phone_correction_prevents_booking_even_when_model_tries(make_session, calendar, first_window):
    session, model = make_session(
        call("find_available_slots", earliest_day=str(first_window.start.date())),
        say("Confirm?"),
        call("book_service_call", slot_id=first_window.id),
        say("Could you repeat your number?"),
        details=[COMPLETE, {"phone": "555"}],
    )
    await talk(session, "Here are my details")
    await talk(session, "Correction: my number is 555")
    assert "3 digits" in model.received[-1][-1].content
    assert calendar.events == []


def test_urgent_reporting_requires_validated_address_then_sends_complete_address(context):
    state = {"call_id": "CA-test", **accept_details({}, {**COMPLETE, "zip_code": ""}, context.maps)}
    runtime = SimpleNamespace(context=context, state=state)
    assert "ZIP code" in report_urgent_issue.func(runtime)
    assert context.email.sent == []
    state.update(accept_details(state, {"zip_code": "80202"}, context.maps))
    assert report_urgent_issue.func(runtime).startswith("Sent:")
    assert "12 Oak St, Denver, 80202" in context.email.sent[0][2]
    assert context.storage.urgent_requests()[0]["zip_code"] == "80202"
