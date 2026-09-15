from datetime import date, datetime

import pytest

from voice_agent.agent.services import schedule
from voice_agent.agent.services.storage import Technician
from voice_agent.settings import Settings


@pytest.fixture
def weekday_settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, business_start_hour=8, business_end_hour=17, service_call_minutes=120)


def test_windows_follow_business_days_and_hours_and_skip_the_past(weekday_settings):
    tz = weekday_settings.timezone
    now = datetime(2026, 9, 18, 9, 0, tzinfo=tz)  # Friday 9 AM
    windows = schedule.windows(weekday_settings, date(2026, 9, 18), date(2026, 9, 21), now)

    assert [w.id for w in windows] == [
        "2026-09-18 10:00",
        "2026-09-18 12:00",
        "2026-09-18 14:00",
        # Saturday and Sunday skipped; 16:00-18:00 would end after closing
        "2026-09-21 08:00",
        "2026-09-21 10:00",
        "2026-09-21 12:00",
        "2026-09-21 14:00",
    ]


def test_window_label_is_spoken_without_time_zone(weekday_settings):
    window = schedule.window_from_id("2026-09-16 10:15", weekday_settings)
    assert window.label() == "Wednesday, September 16, at 10:15 AM"
    assert schedule.spoken_time(datetime(2026, 9, 16, 13, 30, tzinfo=weekday_settings.timezone)) == "1:30 PM"


def test_unreadable_or_overlapping_calendars_are_not_free(weekday_settings):
    window = schedule.window_from_id("2026-09-16 10:00", weekday_settings)
    ana, ben, cy = Technician("Ana", "ana"), Technician("Ben", "ben"), Technician("Cy", "cy")
    busy = {
        "ana": [(window.start.replace(hour=11), window.start.replace(hour=13))],  # overlaps
        "ben": [(window.start.replace(hour=12), window.start.replace(hour=13))],  # adjacent, fine
        # "cy" missing: its calendar could not be read
    }
    assert schedule.free_technicians(window, [ana, ben, cy], busy) == [ben]


def test_least_busy_technician_is_chosen(weekday_settings):
    start = datetime(2026, 9, 16, 8, tzinfo=weekday_settings.timezone)
    ana, ben = Technician("Ana", "ana"), Technician("Ben", "ben")
    busy = {"ana": [(start, start.replace(hour=12))], "ben": [(start, start.replace(hour=9))]}
    assert schedule.least_busy([ana, ben], busy) == ben
