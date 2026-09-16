"""Tools for finding and booking service calls: a short phone call from a technician to the customer."""

from datetime import date, datetime, time, timedelta
from uuid import uuid4

import structlog
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from voice_agent.agent.details import DETAILS, details_needed, full_address
from voice_agent.agent.services import schedule
from voice_agent.agent.services.composio import ComposioError
from voice_agent.agent.services.storage import Technician
from voice_agent.agent.state import AgentContext, CallState

MAX_OFFERED_WINDOWS = 6
CALENDAR_UNAVAILABLE = "The calendar can't be reached right now. Apologize and offer to try again in a moment."

logger = structlog.get_logger()


@tool(parse_docstring=True)
def find_available_slots(
    runtime: ToolRuntime[AgentContext, CallState],
    earliest_day: str = "",
    latest_day: str = "",
    earliest_time: str = "",
    latest_time: str = "",
    service_call_id: str = "",
) -> Command:
    """Find open times, earliest first. New bookings need accepted details; rescheduling uses the stored booking.

    Args:
        earliest_day: First day to search, as YYYY-MM-DD. Leave empty to start today.
        latest_day: Last day to search, as YYYY-MM-DD. Leave empty to search one week from earliest_day.
        earliest_time: Earliest time of day the caller wants, as HH:MM. Leave empty for any time.
        latest_time: Latest time of day the caller wants, as HH:MM. Leave empty for any time.
        service_call_id: For rescheduling, the booking ID from find_my_service_calls. Leave empty for a new booking.
    """

    def search_result(text: str, **updates) -> Command:
        return result(
            runtime,
            text,
            offered_slots=updates.pop("offered_slots", []),
            offered_for_service_call=service_call_id,
            **updates,
        )

    context = runtime.context
    settings = context.settings
    now = datetime.now(settings.timezone)
    if service_call_id:
        if service_call_id not in runtime.state.get("found_service_calls", []):
            return search_result(
                "Not searched: that service_call_id isn't in the latest lookup. Look up the caller's calls first."
            )
        rows = context.storage.service_calls(id=service_call_id, status="booked")
        if not rows:
            return search_result("Not searched: that call is no longer booked.")
        known = rows[0]
    else:
        known = runtime.state.get("facts", {})
        if missing := details_needed(runtime.state):
            logger.info("missing details", call_id=runtime.state["call_id"], missing=missing, facts=known)
            return search_result(f"Not searched: {missing}")
    known_address = full_address(known)
    technicians = serving_technicians(context, known_address)
    if not technicians:
        return search_result("No technician covers that address. Tell the caller it is outside our service area.")

    try:
        first = date.fromisoformat(earliest_day) if earliest_day else now.date()
        last = date.fromisoformat(latest_day) if latest_day else first + timedelta(days=6)
        wanted = (
            time.fromisoformat(earliest_time) if earliest_time else time.min,
            time.fromisoformat(latest_time) if latest_time else time.max,
        )
    except ValueError:
        return search_result("Days must be written as YYYY-MM-DD and times of day as HH:MM, e.g. 14:00.")

    horizon = now.date() + timedelta(days=settings.booking_horizon_days)
    first, last = max(first, now.date()), min(last, horizon)
    candidates = schedule.windows(settings, first, last, now)
    if not candidates:
        return search_result(
            f"No business hours left between {first} and {last}. Offer the next open day instead, and don't say a "
            f"call is scheduled. Bookings are open until {horizon}.",
        )
    try:
        busy = context.calendar.busy([t.calendar_id for t in technicians], candidates[0].start, candidates[-1].end)
    except ComposioError as error:
        logger.error("calendar_search_failed", call_id=runtime.state["call_id"], error=str(error))
        return search_result(CALENDAR_UNAVAILABLE)
    if not busy:
        return search_result(CALENDAR_UNAVAILABLE)

    wanted_time = [w for w in candidates if wanted[0] <= w.start.time() <= wanted[1]]
    open_windows = [w for w in wanted_time if schedule.free_technicians(w, technicians, busy)][:MAX_OFFERED_WINDOWS]
    if not open_windows:
        asked = (
            f" between {earliest_time or 'any time'} and {latest_time or 'any time'}"
            if earliest_time or latest_time
            else ""
        )
        return search_result(f"No open call times{asked} between {first} and {last}. Offer other days or times.")
    lines = "\n".join(f"{window.id}: {window.label()}" for window in open_windows)
    address_note = (
        f"Replacement times for {service_call_id}, using its stored address: {known_address}. Customer details stay unchanged."
        if service_call_id
        else f"Address used: {known_address}. Confirm it once if not already confirmed; if it is wrong, search again."
    )
    return search_result(
        address_note + f"\nOpen call times, earliest first (slot_id: how to say it):\n{lines}",
        offered_slots=[window.id for window in open_windows],
    )


@tool(parse_docstring=True)
def book_service_call(slot_id: str, runtime: ToolRuntime[AgentContext, CallState]) -> str:
    """Schedule a technician to call the customer, using the details collected on this call.

    Args:
        slot_id: A slot_id from the latest find_available_slots result.
    """
    context = runtime.context
    state = runtime.state
    call_id = state["call_id"]
    facts = state.get("facts", {})
    if missing := details_needed(state):
        logger.info("missing details", call_id=call_id, missing=missing, facts=facts)
        return f"Not booked: {missing}"
    if state.get("offered_for_service_call"):
        return "Not booked: those times were searched for an existing call. Search new-booking times first."
    details = {name: facts[name] for name in DETAILS}
    if slot_id not in state.get("offered_slots", []):
        return "Not booked: that slot_id is not in the latest search. Search again and offer one of the results."
    window = schedule.window_from_id(slot_id, context.settings)

    with context.storage.lock:
        if context.storage.urgent_requests(call_id=call_id):
            return "Not booked: this call was already reported as urgent and the team was emailed. Don't book a call."
        existing = context.storage.service_calls(call_id=call_id, status="booked")
        if existing:
            booked = existing[0]
            return (
                f"Already booked on this call: service call {booked['id']} with {booked['technician']}, "
                f"{booked['start']}. To change it, use find_my_service_calls and reschedule_service_call."
            )

        try:
            technician = pick_technician(
                context, window, technicians=serving_technicians(context, full_address(details))
            )
            if technician is None:
                return "Not booked: that time was just taken. Search again and offer new times."
            event_id = context.calendar.create_event(
                technician.calendar_id, window.start, window.end, **event_text(details)
            )
        except ComposioError as error:
            logger.error("booking_failed", call_id=call_id, slot_id=slot_id, error=str(error))
            return "Not booked: " + CALENDAR_UNAVAILABLE

        service_call_id = f"SC-{uuid4().hex[:8].upper()}"
        context.storage.add_service_call(
            {
                "id": service_call_id,
                "created_at": datetime.now(context.settings.timezone).isoformat(timespec="seconds"),
                "call_id": call_id,
                "status": "booked",
                **details,
                "calling_from": state.get("caller_phone", ""),
                "technician": technician.name,
                "calendar_id": technician.calendar_id,
                "event_id": event_id,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
            }
        )

    logger.info("service_call_booked", call_id=call_id, service_call_id=service_call_id, technician=technician.name)
    return f"Booked: {technician.name} will call {details['phone']} {window.label()} (service call {service_call_id})."


def serving_technicians(context: AgentContext, address: str) -> list[Technician]:
    """Technicians whose home base is within the service radius of the address.

    An address or home base Google can't place doesn't exclude anyone: an unusable map must not block booking.
    """
    technicians = context.storage.technicians()
    customer = context.maps.locate(address)
    if customer is None:
        return technicians
    radius = context.settings.service_radius_miles
    return [
        technician
        for technician in technicians
        if (base := context.maps.locate(technician.address)) is None or base.miles_to(customer) <= radius
    ]


def pick_technician(
    context: AgentContext, window: schedule.Window, prefer: str = "", technicians: list[Technician] | None = None
) -> Technician | None:
    """Recheck the calendars and choose a free technician for the window: `prefer` (a calendar ID) if free,
    otherwise the least busy that day. None if everyone is busy. Call it while holding the storage lock."""
    technicians = context.storage.technicians() if technicians is None else technicians
    day_start = window.start.replace(hour=0, minute=0)
    busy = context.calendar.busy([t.calendar_id for t in technicians], day_start, day_start + timedelta(days=1))
    free = schedule.free_technicians(window, technicians, busy)
    preferred = [t for t in free if t.calendar_id == prefer]
    return preferred[0] if preferred else (schedule.least_busy(free, busy) if free else None)


def event_text(details: dict[str, str]) -> dict[str, str]:
    """Calendar event title and description for a service call."""
    return {
        "summary": f"Call {details['customer_name']}: {details['issue']}"[:120],
        "description": (
            f"Call the customer at {details['phone']}.\nCustomer: {details['customer_name']}\n"
            f"Address: {full_address(details)}\nIssue: {details['issue']}"
        ),
    }


def result(runtime: ToolRuntime, text: str, **state_updates) -> Command:
    """Answer the model and optionally update call state in the same step."""
    return Command(update={"messages": [ToolMessage(text, tool_call_id=runtime.tool_call_id)], **state_updates})
