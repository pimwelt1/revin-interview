"""What the graph remembers during a call (CallState) and what it is given (AgentContext)."""

from dataclasses import dataclass
from typing import Literal

from langgraph.graph import MessagesState

from voice_agent.agent.services.calendar import ComposioCalendar
from voice_agent.agent.services.email import ComposioEmail
from voice_agent.agent.services.maps import Maps
from voice_agent.agent.services.storage import Storage
from voice_agent.settings import Settings


class CallState(MessagesState):
    call_id: str
    turn_id: int  # the caller turn being handled; tools use it to tell one turn from the next
    caller_phone: str
    language: Literal["en", "es"]
    facts: dict[str, str]  # validated details only; address is accepted with its city and ZIP
    pending_details: dict[str, str]  # incomplete or invalid proposals, never used by booking tools
    detail_errors: dict[str, str]  # reasons consumed by the single missing-details feedback block
    offered_slots: list[str]  # window IDs from the latest search; booking only accepts these
    offered_for_service_call: str  # booking whose replacement times were searched; empty for new bookings
    found_service_calls: list[str]  # service call IDs from the latest lookup; changes only accept these
    found_on_turn: int  # when they were listed: a change must come after the caller heard them
    call_finished: bool  # set by end_call


@dataclass
class AgentContext:
    settings: Settings
    calendar: ComposioCalendar
    email: ComposioEmail
    maps: Maps
    storage: Storage
