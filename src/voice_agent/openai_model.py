"""OpenAI Responses text streaming."""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from voice_agent.conversation import Turn
from voice_agent.prompts import CONVERSATION_PROMPT
from voice_agent.settings import Settings


def transcript(history: list[Turn]) -> list[dict[str, str]]:
    messages = []
    for turn in history:
        messages.append({"role": "user", "content": turn.caller})
        if turn.interrupted:
            text = (turn.spoken or "") + " [Response interrupted; remaining playback unknown.]"
        else:
            text = turn.response
        if text:
            messages.append({"role": "assistant", "content": text})
    return messages


class OpenAIModel:
    def __init__(self, settings: Settings):
        self.model = settings.openai_model
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.model_timeout_seconds,
            max_retries=0,
        )

    async def close(self) -> None:
        await self.client.close()

    async def stream(self, history: list[Turn], language: str) -> AsyncIterator[str]:
        stream = await self.client.responses.create(
            model=self.model,
            instructions=CONVERSATION_PROMPT + f"\nCurrent response language: {language}.",
            input=transcript(history),
            stream=True,
            store=False,
        )
        completed = False
        try:
            async for event in stream:
                if event.type == "response.output_text.delta" and event.delta:
                    yield event.delta
                elif event.type == "response.completed":
                    completed = True
                elif event.type in {"error", "response.failed", "response.incomplete", "response.refusal.done"}:
                    raise ValueError("OpenAI reply failed, was incomplete, or was refused")
        finally:
            await stream.close()
        if not completed:
            raise ValueError("OpenAI reply stream ended without completion")
