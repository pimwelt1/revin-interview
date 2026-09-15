"""Tools for finding and booking service calls: a short phone call from a technician to the customer."""

from datetime import date, datetime, timedelta
from uuid import uuid4

import structlog
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from voice_agent.agent.services import schedule
from voice_agent.agent.services.composio import ComposioError
from voice_agent.agent.services.storage import Technician
from voice_agent.agent.state import AgentContext, CallState

MAX_OFFERED_WINDOWS = 6
CALENDAR_UNAVAILABLE = "The calendar can't be reached right now. Apologize and offer to try again in a moment."

logger = structlog.get_logger()


@tool(parse_docstring=True)
def find_available_slots(
    runtime: ToolRuntime[AgentContext, CallState], earliest_day: str = "", latest_day: str = ""
) -> Command:
    """Find open times for a technician to call the customer, earliest first.

    Args:
        earliest_day: First day to search, as YYYY-MM-DD. Leave empty to start today.
        latest_day: Last day to search, as YYYY-MM-DD. Leave empty to search one week from earliest_day.
    """
    context = runtime.context
    settings = context.settings
    now = datetime.now(settings.timezone)
    try:
        first = date.fromisoformat(earliest_day) if earliest_day else now.date()
        last = date.fromisoformat(latest_day) if latest_day else first + timedelta(days=6)
    except ValueError:
        return result(runtime, "Days must be written as YYYY-MM-DD.")

    horizon = now.date() + timedelta(days=settings.booking_horizon_days)
    first, last = max(first, now.date()), min(last, horizon)
    candidates = schedule.windows(settings, first, last, now)
    if not candidates:
        return result(runtime, f"No business hours between {first} and {last}. Bookings are open until {horizon}.")

    technicians = context.storage.technicians()
    try:
        busy = context.calendar.busy([t.calendar_id for t in technicians], candidates[0].start, candidates[-1].end)
    except ComposioError as error:
        logger.error("calendar_search_failed", call_id=runtime.state["call_id"], error=str(error))
        return result(runtime, CALENDAR_UNAVAILABLE)
    if not busy:
        return result(runtime, CALENDAR_UNAVAILABLE)

    open_windows = [w for w in candidates if schedule.free_technicians(w, technicians, busy)][:MAX_OFFERED_WINDOWS]
    if not open_windows:
        return result(runtime, f"No open call times between {first} and {last}. Offer to look at later days.")
    lines = "\n".join(f"{window.id}: {window.label()}" for window in open_windows)
    return result(
        runtime,
        f"Open call times, earliest first (slot_id: how to say it):\n{lines}",
        offered_slots=[window.id for window in open_windows],
    )


@tool(parse_docstring=True)
def book_service_call(
    slot_id: str,
    customer_name: str,
    phone: str,
    address: str,
    issue: str,
    runtime: ToolRuntime[AgentContext, CallState],
) -> str:
    """Schedule a technician to call the customer. Use only after the caller confirmed your read-back of these details.

    Args:
        slot_id: A slot_id from the latest find_available_slots result.
        customer_name: The caller's full name.
        phone: The number the technician should call.
        address: The full service address.
        issue: A short description of the problem and the equipment.
    """
    context = runtime.context
    state = runtime.state
    call_id = state["call_id"]
    details = {"customer_name": customer_name, "phone": phone, "address": address, "issue": issue}
    missing = [name for name, value in details.items() if not value.strip()]
    if missing:
        return "Not booked. Missing: " + ", ".join(missing) + "."
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
            technician = pick_technician(context, window)
            if technician is None:
                return "Not booked: that time was just taken. Search again and offer new times."
            event_id = context.calendar.create_event(technician.calendar_id, window.start, window.end, **event_text(details))
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
    return f"Booked: {technician.name} will call {phone} on {window.label()} (service call {service_call_id})."


def pick_technician(context: AgentContext, window: schedule.Window, prefer: str = "") -> Technician | None:
    """Recheck the calendars and choose a free technician for the window: `prefer` (a calendar ID) if free,
    otherwise the least busy that day. None if everyone is busy. Call it while holding the storage lock."""
    technicians = context.storage.technicians()
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
            f"Address: {details['address']}\nIssue: {details['issue']}"
        ),
    }


def result(runtime: ToolRuntime, text: str, **state_updates) -> Command:
    """Answer the model and optionally update call state in the same step."""
    return Command(update={"messages": [ToolMessage(text, tool_call_id=runtime.tool_call_id)], **state_updates})
