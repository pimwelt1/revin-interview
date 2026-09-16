import json
from datetime import datetime, timedelta
from itertools import count
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.memory import InMemorySaver

from voice_agent.agent.graph import build_graph
from voice_agent.agent.services import schedule
from voice_agent.agent.services.calendar import Interval
from voice_agent.agent.services.composio import ComposioTools
from voice_agent.agent.services.maps import Location, Maps
from voice_agent.agent.services.storage import Storage
from voice_agent.agent.state import AgentContext
from voice_agent.session import Session
from voice_agent.settings import Settings

TECHNICIANS = [("Ana", "ana@calendar"), ("Ben", "ben@calendar")]
ADDRESS = "12 Oak St, Denver, 80202"
DENVER = Location(39.7392, -104.9903)


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
    """Replies with the given messages in order and records what it was sent.

    The graph binds its one model twice, so each binding gets its own script: `bind_tools` for the agent
    that speaks, and `bind(response_format=...)` for the detail extractor in `details`.
    """

    received: list
    repeat_last: bool = False
    details: Any = None

    def bind_tools(self, tools, **kwargs):
        return self

    def bind(self, **kwargs):
        return self.details if "response_format" in kwargs and self.details is not None else self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(messages)
        last = self.i == len(self.responses) - 1
        result = super()._generate(messages, stop, run_manager, **kwargs)
        if last and self.repeat_last:
            self.i = len(self.responses) - 1
        return result


class StreamingModel(ScriptedModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._generate(messages, stop, run_manager, **kwargs).generations[0].message
        identifier = uuid4().hex
        for character in response.text:
            yield ChatGenerationChunk(message=AIMessageChunk(character, id=identifier))
        if response.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    "",
                    id=identifier,
                    tool_call_chunks=[
                        {"name": call["name"], "args": json.dumps(call["args"]), "id": call["id"], "index": index}
                        for index, call in enumerate(response.tool_calls)
                    ],
                )
            )


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


def geocode_result(address="12 Oak St", city="Denver", zip_code="80202") -> dict:
    number, route = address.split(" ", 1)
    return {
        "geometry": {"location": {"lat": DENVER.latitude, "lng": DENVER.longitude}},
        "address_components": [
            {"long_name": value, "short_name": value, "types": [kind]}
            for kind, value in (
                ("street_number", number),
                ("route", route),
                ("locality", city),
                ("postal_code", zip_code),
                ("country", "US"),
            )
        ],
    }


class FakeMaps(Maps):
    def __init__(self):
        super().__init__(None)
        self.requests = []
        self.responses = {
            "12 Oak St, Denver, 80202, USA": [geocode_result()],
            "161 W 54th St, New York, 10019, USA": [geocode_result("161 W 54th St", "New York", "10019")],
        }

    def _results(self, address):
        self.requests.append(address)
        response = self.responses[address]
        if isinstance(response, Exception):
            raise response
        return response


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))


@pytest.fixture(autouse=True)
def no_provider_calls(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Tests must not execute real Composio tools")

    monkeypatch.setattr(ComposioTools, "execute", unexpected)


@pytest.fixture
def maps() -> FakeMaps:
    return FakeMaps()


@pytest.fixture
def context(settings, storage, calendar, maps) -> AgentContext:
    return AgentContext(settings=settings, calendar=calendar, email=FakeEmail(), maps=maps, storage=storage)


@pytest.fixture
def first_window(settings) -> schedule.Window:
    """The first window find_available_slots offers when searching from tomorrow."""
    now = datetime.now(settings.timezone)
    tomorrow = now.date() + timedelta(days=1)
    return schedule.windows(settings, tomorrow, tomorrow, now)[0]


@pytest.fixture
def make_session(context):
    def make(
        *responses: AIMessage,
        call_id: str = "CA-test",
        details: tuple[dict, ...] | list[dict] = (),
        streaming: bool = False,
    ) -> tuple[Session, ScriptedModel]:
        model_class = StreamingModel if streaming else ScriptedModel
        extractor = ScriptedModel(
            responses=[AIMessage(json.dumps(values)) for values in details] + [AIMessage("{}")],
            received=[],
            repeat_last=True,
        )
        model = model_class(responses=list(responses), received=[], details=extractor)
        graph = build_graph(model, InMemorySaver())
        return Session(call_id, graph, context, caller_phone="+15555550123"), model

    return make


async def talk(session: Session, text: str, language: str = "") -> str:
    turn = session.start_turn(text, language)
    return "".join([token async for token in session.reply(turn)])
