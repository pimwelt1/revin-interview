"""Tools for a caller's existing service calls: look them up, reschedule, or cancel.

A caller can only change calls found for the number they are calling from, in this phone call.
"""

from datetime import datetime

import structlog
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from voice_agent.agent.details import full_address
from voice_agent.agent.services import schedule
from voice_agent.agent.services.composio import ComposioError
from voice_agent.agent.state import AgentContext, CallState
from voice_agent.agent.tools.scheduling import (
    CALENDAR_UNAVAILABLE,
    event_text,
    pick_technician,
    result,
    serving_technicians,
)

NOT_FOUND = "that service_call_id isn't in the latest find_my_service_calls result. Look the caller's calls up again."
TOO_SOON = "you listed the caller's calls this turn. Read the call back, and change it after the caller confirms."

logger = structlog.get_logger()


@tool
def find_my_service_calls(runtime: ToolRuntime[AgentContext, CallState]) -> Command:
    """Find the caller's upcoming technician calls, booked from or for the number they are calling from."""
    context, state = runtime.context, runtime.state
    caller_phone = state.get("caller_phone", "")
    timezone = context.settings.timezone
    now = datetime.now(timezone)
    upcoming = sorted(
        (
            row
            for row in context.storage.service_calls_for_phone(caller_phone)
            if datetime.fromisoformat(row["end"]) > now
        ),
        key=lambda row: row["start"],
    )
    if not upcoming:
        return result(
            runtime,
            f"No upcoming technician calls found for {caller_phone or 'an unknown number'}. Calls booked under "
            "another number can't be looked up. Offer to schedule a new call.",
            found_service_calls=[],
        )
    lines = "\n".join(
        f"{row['id']}: {schedule.Window.from_row(row, timezone).label()}, about {row['issue']}, "
        f"calling {row['phone']}"
        for row in upcoming
    )
    return result(
        runtime,
        f"Upcoming technician calls (service_call_id: when, issue, number):\n{lines}",
        found_service_calls=[row["id"] for row in upcoming],
        found_on_turn=state.get("turn_id", 0),
    )


@tool(parse_docstring=True)
def reschedule_service_call(service_call_id: str, slot_id: str, runtime: ToolRuntime[AgentContext, CallState]) -> str:
    """Move only the time of a technician call, keeping its stored customer details. Requires caller confirmation.

    Args:
        service_call_id: A service_call_id from the latest find_my_service_calls result.
        slot_id: The new time, a slot_id from the latest find_available_slots result.
    """
    context, state = runtime.context, runtime.state
    if service_call_id not in state.get("found_service_calls", []):
        return "Not changed: " + NOT_FOUND
    if state.get("found_on_turn") == state.get("turn_id"):
        return "Not changed: " + TOO_SOON
    if slot_id not in state.get("offered_slots", []):
        return "Not changed: that slot_id is not in the latest search. Search again and offer one of the results."
    if state.get("offered_for_service_call") != service_call_id:
        return "Not changed: search replacement times for this service_call_id first."
    window = schedule.window_from_id(slot_id, context.settings)

    with context.storage.lock:
        rows = context.storage.service_calls(id=service_call_id, status="booked")
        if not rows:
            return "Not changed: that call is no longer booked."
        booked = rows[0]
        if datetime.fromisoformat(booked["start"]) == window.start:
            return "Not changed: the call is already at that time."
        try:
            # Keep the same technician when they're free; otherwise move the call to another technician's calendar.
            technician = pick_technician(
                context,
                window,
                prefer=booked["calendar_id"],
                technicians=serving_technicians(context, full_address(booked)),
            )
            if technician is None:
                return "Not changed: that time was just taken. Search again and offer new times."
            if technician.calendar_id == booked["calendar_id"]:
                event_id = booked["event_id"]
                context.calendar.move_event(technician.calendar_id, event_id, window.start, window.end)
            else:
                event_id = context.calendar.create_event(
                    technician.calendar_id, window.start, window.end, **event_text(booked)
                )
                delete_old_event(context, booked)
        except ComposioError as error:
            logger.error(
                "reschedule_failed", call_id=state["call_id"], service_call_id=service_call_id, error=str(error)
            )
            return "Not changed: " + CALENDAR_UNAVAILABLE

        context.storage.update_service_call(
            service_call_id,
            technician=technician.name,
            calendar_id=technician.calendar_id,
            event_id=event_id,
            start=window.start.isoformat(),
            end=window.end.isoformat(),
        )

    logger.info("service_call_rescheduled", call_id=state["call_id"], service_call_id=service_call_id)
    return f"Rescheduled: {technician.name} will now call {booked['phone']} {window.label()}."


@tool(parse_docstring=True)
def cancel_service_call(service_call_id: str, runtime: ToolRuntime[AgentContext, CallState]) -> str:
    """Cancel a technician call. Use only after the caller confirmed the cancellation.

    Args:
        service_call_id: A service_call_id from the latest find_my_service_calls result.
    """
    context, state = runtime.context, runtime.state
    if service_call_id not in state.get("found_service_calls", []):
        return "Not cancelled: " + NOT_FOUND
    if state.get("found_on_turn") == state.get("turn_id"):
        return "Not cancelled: " + TOO_SOON

    with context.storage.lock:
        rows = context.storage.service_calls(id=service_call_id, status="booked")
        if not rows:
            return "Already cancelled: that call is no longer booked."
        booked = rows[0]
        try:
            context.calendar.delete_event(booked["calendar_id"], booked["event_id"])
        except ComposioError as error:
            logger.error("cancel_failed", call_id=state["call_id"], service_call_id=service_call_id, error=str(error))
            return "Not cancelled: " + CALENDAR_UNAVAILABLE
        context.storage.update_service_call(service_call_id, status="cancelled")

    spoken = schedule.Window.from_row(booked, context.settings.timezone).label()
    logger.info("service_call_cancelled", call_id=state["call_id"], service_call_id=service_call_id)
    return f"Cancelled: the technician call {spoken} is cancelled."


def delete_old_event(context: AgentContext, booked: dict[str, str]) -> None:
    """After moving a call to another technician, remove the old event. The move already succeeded, so a failure
    here is logged for staff to clean up rather than undoing the new booking."""
    try:
        context.calendar.delete_event(booked["calendar_id"], booked["event_id"])
    except ComposioError as error:
        logger.error(
            "old_event_not_deleted", service_call_id=booked["id"], event_id=booked["event_id"], error=str(error)
        )
