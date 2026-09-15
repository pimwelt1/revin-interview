"""One phone call: track turns and interruptions, and stream the agent graph's reply.

The transcript lives in the graph's checkpointer (thread_id = call_id). This class only remembers
what the phone layer knows and the graph doesn't: which reply is current and what the caller actually heard.
"""

from collections.abc import AsyncIterator
from contextlib import aclosing
from itertools import pairwise
from time import monotonic

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from voice_agent.agent.state import AgentContext
from voice_agent.logging_config import error_details
from voice_agent.prompts import GREETING

# Bounds the agent ⇄ tools loop within one caller turn.
RECURSION_LIMIT = 12
TOOL_INTERRUPTED = "The caller interrupted before this finished; the outcome is unknown. Call the tool again if needed."


class Turn(BaseModel):
    id: int
    caller: str
    response: str = ""
    interrupted: bool = False
    spoken: str | None = None


class Session:
    def __init__(self, call_id: str, graph: CompiledStateGraph, context: AgentContext, caller_phone: str = ""):
        self.call_id, self.graph, self.context, self.caller_phone = call_id, graph, context, caller_phone
        self.config = {"configurable": {"thread_id": call_id}, "recursion_limit": RECURSION_LIMIT}
        self.turn: Turn | None = None
        self.previous_turn: Turn | None = None
        self.phone_notes: list[str] = [GREETING]  # said by the phone layer, not yet in the transcript
        self.language = "en"
        self.language_votes: list[str] = []  # languages of the caller turns that counted, in order
        self.failed = self.finished = False
        self.logger = structlog.get_logger().bind(call_id=call_id)

    # Defensive concurrency checkpoint to ensure the turn being processed is the latest and has not been interrupted.
    def is_current(self, turn: Turn) -> bool:
        return turn is self.turn and not turn.interrupted

    def start_turn(self, text: str, language: str = "") -> Turn:
        """Start a caller turn. `language` is Twilio's language hint for this utterance (e.g. "es-MX")."""
        self.language = self.choose_language(text, language)
        self.previous_turn = self.turn
        self.turn = Turn(id=self.turn.id + 1 if self.turn else 1, caller=text)
        self.logger.info("caller_turn", turn_id=self.turn.id, text=text, language=self.language)
        return self.turn

    def interrupt(self, spoken: str | None = None) -> None:
        if self.turn:
            turn = self.turn
            turn.interrupted = True
            turn.spoken = spoken if spoken is not None and turn.response.startswith(spoken) else None
            self.logger.info("turn_interrupted", turn_id=turn.id, spoken_text=turn.spoken, generated_text=turn.response)

    def note_silence_prompt(self, text: str) -> None:
        """Keep the 'are you there?' prompt so the model knows it was said."""
        self.phone_notes.append(text)

    async def close(self) -> None:
        """Forget the transcript; bookings are already in the calendar and CSV."""
        if self.graph.checkpointer:
            await self.graph.checkpointer.adelete_thread(self.call_id)

    #####       Main reply function     #####
    async def reply(self, turn: Turn) -> AsyncIterator[str]:
        if self.failed or self.finished:
            return
        started = monotonic()
        try:
            async with aclosing(self.run_agent(turn)) as replies:
                async for text in replies:
                    if not self.is_current(turn):
                        return
                    if not turn.response:
                        self.logger.info("first_token", turn_id=turn.id, elapsed_ms=round((monotonic() - started) * 1000))
                    turn.response += text
                    yield text
        except Exception as error:  # noqa: BLE001 - any agent failure must end the call cleanly
            if self.is_current(turn):
                self.failed = True
                self.logger.error("system_error", turn_id=turn.id, stage="agent", **error_details(error))
        finally:
            self.logger.info("agent_response", turn_id=turn.id, source="agent", text=turn.response, interrupted=turn.interrupted)
            self.logger.info("turn_finished", turn_id=turn.id, elapsed_ms=round((monotonic() - started) * 1000))

    async def run_agent(self, turn: Turn) -> AsyncIterator[str]:
        """Run the graph once for this utterance and yield the agent's spoken text."""
        inputs = {
            "messages": [*await self.catch_up_messages(), HumanMessage(turn.caller)],
            "call_id": self.call_id,
            "caller_phone": self.caller_phone,
            "language": self.language,
        }
        self.phone_notes = []
        stream = self.graph.astream(inputs, self.config, context=self.context, stream_mode="messages")
        async with aclosing(stream) as chunks:
            async for message, metadata in chunks:
                if isinstance(message, ToolMessage):
                    self.logger.info("tool_result", turn_id=turn.id, tool=message.name)
                elif metadata["langgraph_node"] == "agent" and isinstance(message, AIMessage) and message.content:
                    yield message.text
        state = await self.graph.aget_state(self.config)
        self.finished = bool(state.values.get("call_finished"))

    async def catch_up_messages(self) -> list[BaseMessage]:
        """Messages that make the saved transcript match what actually happened on the phone."""
        messages = (await self.graph.aget_state(self.config)).values.get("messages", [])
        last = messages[-1] if messages else None
        updates: list[BaseMessage] = []

        # A cancelled run can leave tool calls without results; the model API rejects that history.
        if isinstance(last, AIMessage) and last.tool_calls:
            updates += [ToolMessage(TOOL_INTERRUPTED, tool_call_id=call["id"]) for call in last.tool_calls]

        previous = self.previous_turn
        if previous and previous.interrupted:
            heard = f"{previous.spoken} [interrupted]" if previous.spoken else "[interrupted before anything was heard]"
            reply_was_saved = isinstance(last, AIMessage) and not last.tool_calls and last.content == previous.response
            if reply_was_saved:
                updates.append(AIMessage(heard, id=last.id))  # same ID replaces the full reply
            elif previous.response:
                updates.append(AIMessage(heard))

        updates += [AIMessage(note) for note in self.phone_notes]
        return updates

    def choose_language(self, text: str, hint: str) -> str:
        """Return the call's language after this caller turn, adding the turn to the votes if it counts.

        A turn counts if it has 3+ words and Twilio's hint is English or Spanish.
        Once two counted turns in a row agree, that language stays until two turns in a row agree on the other one.
        Before that, follow the latest counted turn. If nothing has counted yet, use this turn's hint if it is Spanish,
        otherwise English.
        """
        language = hint.strip().lower()[:2]
        if language in ("en", "es") and len(text.split()) >= 3:
            self.language_votes.append(language)
        agreed = [vote for previous, vote in pairwise(self.language_votes) if vote == previous]
        if agreed:
            return agreed[-1]
        if self.language_votes:
            return self.language_votes[-1]
        return "es" if language == "es" else "en"
