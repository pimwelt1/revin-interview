"""A readable turn loop: record the caller, stream the agent's reply."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from time import monotonic
from typing import Protocol

import structlog
from pydantic import BaseModel

from voice_agent.logging_config import error_details


class Turn(BaseModel):
    id: int
    caller: str
    response: str = ""
    interrupted: bool = False
    spoken: str | None = None


class ConversationModel(Protocol):
    def stream(self, history: list[Turn], language: str) -> AsyncIterator[str]: ...


class Conversation:
    def __init__(self, call_id: str, model: ConversationModel, caller_phone: str = ""):
        self.call_id, self.model, self.caller_phone = call_id, model, caller_phone
        self.history: list[Turn] = []
        self.language = "en"
        self.failed = self.finished = False
        self.logger = structlog.get_logger().bind(call_id=call_id)

    # Defensive concurrency checkpoint to ensure the turn being processed is the latest and has not been interrupted.
    def is_current(self, turn: Turn) -> bool:
        return turn is self.history[-1] and not turn.interrupted

    def start_turn(self, text: str, language: str = "") -> Turn:
        ###LANGUAGE CHECK LOGIQUE
        turn = Turn(id=len(self.history) + 1, caller=text)
        self.history.append(turn)
        self.logger.info("caller_turn", turn_id=turn.id, text=text, language=self.language)
        return turn

    def interrupt(self, spoken: str | None = None) -> None:
        if self.history:
            turn = self.history[-1]
            turn.interrupted = True
            turn.spoken = spoken if spoken is not None and turn.response.startswith(spoken) else None
            self.logger.info("turn_interrupted", turn_id=turn.id, spoken_text=turn.spoken, generated_text=turn.response)


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
        except Exception as error:
            if self.is_current(turn):
                self.failed = True
                self.logger.error("system_error", turn_id=turn.id, stage="agent", **error_details(error))
        finally:
            self.logger.info("agent_response", turn_id=turn.id, source="agent", text=turn.response, interrupted=turn.interrupted)
            self.logger.info("turn_finished", turn_id=turn.id, elapsed_ms=round((monotonic() - started) * 1000))


    # The agent: tools will be executed here. For now, one streamed model reply.
    async def run_agent(self, turn: Turn) -> AsyncIterator[str]:
        async with aclosing(self.model.stream(self.history, self.language)) as tokens:
            async for token in tokens:
                yield token

    def note_silence_prompt(self, text: str) -> None:
        """Keep the 'are you there?' prompt in history so the model knows it was said."""
        if self.history:
            self.history[-1].response += "\n" + text
