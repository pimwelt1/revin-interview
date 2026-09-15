"""Service-call windows: when a technician could call the customer, and who is free."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from voice_agent.agent.services.calendar import Interval
from voice_agent.agent.services.storage import Technician
from voice_agent.settings import Settings

WINDOW_ID_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @property
    def id(self) -> str:
        """Stable, readable ID the model passes back when booking, e.g. '2026-09-16 10:00'."""
        return self.start.strftime(WINDOW_ID_FORMAT)

    def label(self) -> str:
        """How the call time is said aloud, without a time zone."""
        return f"{self.start:%A, %B} {self.start.day}, at {spoken_time(self.start)}"


def spoken_time(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{hour}{minutes} {'AM' if moment.hour < 12 else 'PM'}"


def windows(settings: Settings, first_day: date, last_day: date, now: datetime) -> list[Window]:
    """All service windows during business hours between two days, skipping any that already started."""
    duration = timedelta(minutes=settings.service_call_minutes)
    result = []
    day = first_day
    while day <= last_day:
        if day.weekday() in settings.weekdays:
            start = datetime.combine(day, time(settings.business_start_hour), settings.timezone)
            closing = datetime.combine(day, time(0), settings.timezone) + timedelta(hours=settings.business_end_hour)
            while start + duration <= closing:
                if start > now:
                    result.append(Window(start, start + duration))
                start += duration
        day += timedelta(days=1)
    return result


def window_from_id(window_id: str, settings: Settings) -> Window:
    start = datetime.strptime(window_id, WINDOW_ID_FORMAT).replace(tzinfo=settings.timezone)
    return Window(start, start + timedelta(minutes=settings.service_call_minutes))


def free_technicians(window: Window, technicians: list[Technician], busy: dict[str, list[Interval]]) -> list[Technician]:
    """Technicians whose calendar was read and has nothing overlapping the window."""
    return [
        technician
        for technician in technicians
        if technician.calendar_id in busy
        and not any(start < window.end and end > window.start for start, end in busy[technician.calendar_id])
    ]


def least_busy(technicians: list[Technician], busy: dict[str, list[Interval]]) -> Technician:
    """Spread the work: pick the technician with the fewest busy minutes in the queried period."""

    def busy_minutes(technician: Technician) -> float:
        return sum((end - start).total_seconds() for start, end in busy.get(technician.calendar_id, [])) / 60

    return min(technicians, key=busy_minutes)
