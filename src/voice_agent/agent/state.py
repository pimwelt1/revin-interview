"""What the graph remembers during a call (CallState) and what it is given (AgentContext)."""

from dataclasses import dataclass
from typing import Literal

from langgraph.graph import MessagesState

from voice_agent.agent.services.calendar import Calendar
from voice_agent.agent.services.email import ComposioEmail
from voice_agent.agent.services.storage import Storage
from voice_agent.settings import Settings


class CallState(MessagesState):
    call_id: str
    caller_phone: str
    language: Literal["en", "es"]
    offered_slots: list[str]  # window IDs from the latest search; booking only accepts these
    found_service_calls: list[str]  # service call IDs from the latest lookup; changes only accept these
    call_finished: bool  # set by end_call


@dataclass
class AgentContext:
    settings: Settings
    calendar: Calendar
    email: ComposioEmail
    storage: Storage
