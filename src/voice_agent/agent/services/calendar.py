"""Technician calendars: read busy times and create events through Composio's Google Calendar tools."""

from datetime import datetime
from typing import Protocol, runtime_checkable

import structlog

from voice_agent.agent.services.composio import ComposioError, ComposioTools, find_key

Interval = tuple[datetime, datetime]


@runtime_checkable
class Calendar(Protocol):
    def busy(self, calendar_ids: list[str], start: datetime, end: datetime) -> dict[str, list[Interval]]:
        """Busy intervals per calendar. Calendars that could not be read are left out, never reported as free."""
        ...

    def create_event(self, calendar_id: str, start: datetime, end: datetime, summary: str, description: str) -> str:
        """Create an event and return its ID."""
        ...

    def move_event(self, calendar_id: str, event_id: str, start: datetime, end: datetime) -> None:
        """Change an event's time on the same calendar."""
        ...

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete an event. Deleting an event that is already gone is not an error."""
        ...


class ComposioCalendar:
    def __init__(self, composio: ComposioTools, timezone: str):
        self.composio = composio
        self.timezone = timezone
        self.logger = structlog.get_logger()

    def busy(self, calendar_ids: list[str], start: datetime, end: datetime) -> dict[str, list[Interval]]:
        data = self.composio.execute(
            "GOOGLECALENDAR_FREE_BUSY_QUERY",
            {"items": calendar_ids, "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": self.timezone},
        )
        calendars = find_key(data, "calendars") or {}
        result = {}
        for calendar_id in calendar_ids:
            entry = calendars.get(calendar_id)
            if not isinstance(entry, dict) or entry.get("errors"):
                self.logger.warning("calendar_unreadable", calendar_id=calendar_id, errors=(entry or {}).get("errors"))
                continue
            result[calendar_id] = [
                (datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"]))
                for block in entry.get("busy", [])
            ]
        return result

    def create_event(self, calendar_id: str, start: datetime, end: datetime, summary: str, description: str) -> str:
        data = self.composio.execute(
            "GOOGLECALENDAR_CREATE_EVENT",
            {
                "calendar_id": calendar_id,
                "start_datetime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "end_datetime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timezone": self.timezone,
                "summary": summary,
                "description": description,
                "send_updates": "none",
                "create_meeting_room": False,
            },
        )
        event_id = find_key(data, "id")
        if not isinstance(event_id, str) or not event_id:
            raise ComposioError("GOOGLECALENDAR_CREATE_EVENT returned no event ID")
        return event_id

    def move_event(self, calendar_id: str, event_id: str, start: datetime, end: datetime) -> None:
        self.composio.execute(
            "GOOGLECALENDAR_PATCH_EVENT",
            {
                "calendar_id": calendar_id,
                "event_id": event_id,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "timezone": self.timezone,
                "send_updates": "none",
            },
        )

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.composio.execute(
            "GOOGLECALENDAR_DELETE_EVENT", {"calendar_id": calendar_id, "event_id": event_id, "send_updates": "none"}
        )
