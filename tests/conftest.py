from datetime import datetime, timedelta
from itertools import count
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from voice_agent.agent.graph import build_graph
from voice_agent.agent.services import schedule
from voice_agent.agent.services.calendar import Interval
from voice_agent.agent.services.composio import ComposioTools
from voice_agent.agent.services.email import ComposioEmail
from voice_agent.agent.services.storage import Storage
from voice_agent.agent.state import AgentContext
from voice_agent.session import Session
from voice_agent.settings import Settings

TECHNICIANS = [("Ana", "ana@calendar"), ("Ben", "ben@calendar")]


class FakeCalendar:
    """In-memory stand-in for ComposioCalendar, so tests never touch Google."""

    def __init__(self, busy: dict[str, list[Interval]] | None = None, unreadable: set[str] = frozenset()):
        self.busy_times = busy or {}
        self.unreadable = set(unreadable)
        self.events: list[dict[str, Any]] = []
        self.ids = count(1)

    def busy(self, calendar_ids: list[str], start: datetime, end: datetime) -> dict[str, list[Interval]]:
        return {
            calendar_id: [(s, e) for s, e in self.busy_times.get(calendar_id, []) if s < end and e > start]
            for calendar_id in calendar_ids
            if calendar_id not in self.unreadable
        }

    def create_event(self, calendar_id: str, start: datetime, end: datetime, summary: str, description: str) -> str:
        event_id = f"fake-event-{next(self.ids)}"
        self.events.append({"id": event_id, "calendar_id": calendar_id, "start": start, "end": end, "summary": summary})
        self.busy_times.setdefault(calendar_id, []).append((start, end))
        return event_id

    def move_event(self, calendar_id: str, event_id: str, start: datetime, end: datetime) -> None:
        event = self.event(event_id)
        self.busy_times[calendar_id].remove((event["start"], event["end"]))
        self.busy_times[calendar_id].append((start, end))
        event.update(start=start, end=end)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        event = self.event(event_id)
        self.busy_times[calendar_id].remove((event["start"], event["end"]))
        self.events.remove(event)

    def event(self, event_id: str) -> dict[str, Any]:
        return next(event for event in self.events if event["id"] == event_id)


class ScriptedModel(FakeMessagesListChatModel):
    """Replies with the given messages in order and records what it was sent."""

    received: list

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(messages)
        return super()._generate(messages, stop, run_manager, **kwargs)


def say(text: str) -> AIMessage:
    return AIMessage(text)


def call(tool: str, **args) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": tool, "args": args, "id": f"call-{tool}-{len(args)}"}])


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, business_weekdays="0,1,2,3,4,5,6")


@pytest.fixture
def storage(settings) -> Storage:
    technicians = settings.data_dir / "technicians.csv"
    technicians.write_text("name,calendar_id\n" + "".join(f"{n},{c}\n" for n, c in TECHNICIANS), encoding="utf-8")
    return Storage(settings.data_dir)


@pytest.fixture
def calendar() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def context(settings, storage, calendar) -> AgentContext:
    # Tests never report urgent issues, so this real client is never called.
    email = ComposioEmail(ComposioTools("unused-in-tests", "tests"), "tests@example.com")
    return AgentContext(settings=settings, calendar=calendar, email=email, storage=storage)


@pytest.fixture
def first_window(settings) -> schedule.Window:
    """The first window find_available_slots offers when searching from tomorrow."""
    now = datetime.now(settings.timezone)
    tomorrow = now.date() + timedelta(days=1)
    return schedule.windows(settings, tomorrow, tomorrow, now)[0]


@pytest.fixture
def make_session(context):
    def make(*responses: AIMessage, call_id: str = "CA-test") -> tuple[Session, ScriptedModel]:
        model = ScriptedModel(responses=list(responses), received=[])
        graph = build_graph(model, InMemorySaver())
        return Session(call_id, graph, context, caller_phone="+15555550123"), model

    return make


async def talk(session: Session, text: str, language: str = "") -> str:
    turn = session.start_turn(text, language)
    return "".join([token async for token in session.reply(turn)])
