"""Validate customer details before the conversational agent speaks or calls a tool."""

import asyncio
import json
from datetime import datetime

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from voice_agent.agent.details import DETAILS_FORMAT, accept_details, describe, read_details
from voice_agent.agent.services.calendar import ComposioCalendar
from voice_agent.agent.services.composio import ComposioTools
from voice_agent.agent.services.email import ComposioEmail
from voice_agent.agent.services.maps import Maps
from voice_agent.agent.services.schedule import spoken_time
from voice_agent.agent.services.storage import Storage
from voice_agent.agent.state import AgentContext, CallState
from voice_agent.agent.tools.call import end_call
from voice_agent.agent.tools.existing_calls import cancel_service_call, find_my_service_calls, reschedule_service_call
from voice_agent.agent.tools.scheduling import book_service_call, find_available_slots
from voice_agent.agent.tools.urgent import report_urgent_issue
from voice_agent.prompts import AGENT_PROMPT, DETAILS_PROMPT
from voice_agent.settings import Settings

TOOLS = [
    find_available_slots,
    book_service_call,
    find_my_service_calls,
    reschedule_service_call,
    cancel_service_call,
    report_urgent_issue,
    end_call,
]

LANGUAGES = {"en": "English", "es": "Spanish"}

logger = structlog.get_logger()


def build_model(settings: Settings) -> BaseChatModel:
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.model_timeout_seconds,
        max_retries=1,
        streaming=True,
    )


def build_context(settings: Settings) -> AgentContext:
    storage = Storage(settings.data_dir)
    storage.technicians()  # fail at startup, not in the middle of a call
    composio = ComposioTools(settings.composio_api_key.get_secret_value(), settings.composio_user_id)
    calendar = ComposioCalendar(composio, settings.business_timezone)
    return AgentContext(
        settings=settings,
        calendar=calendar,
        email=ComposioEmail(composio, settings.email_from),
        maps=Maps(composio),
        storage=storage,
    )


def agent_node(model: BaseChatModel):
    # One tool call per step keeps state updates simple and voice turns predictable.
    model_with_tools = model.bind_tools(TOOLS, parallel_tool_calls=False)

    async def agent(state: CallState, runtime: Runtime[AgentContext]) -> dict:
        now = datetime.now(runtime.context.settings.timezone)
        prompt = AGENT_PROMPT.format(
            call_minutes=runtime.context.settings.service_call_minutes,
            counties=runtime.context.settings.spoken_counties,
            today=f"{now:%A, %B} {now.day}, {now.year}, {spoken_time(now)}",
            language=LANGUAGES[state.get("language", "en")],
            caller_phone=state.get("caller_phone") or "an unknown number",
            details=describe(state),
        )
        response = await model_with_tools.ainvoke([SystemMessage(prompt), *state["messages"]])
        for call in response.tool_calls:
            logger.info("tool_call", call_id=state["call_id"], tool=call["name"], arguments=call["args"])
        return {"messages": [response]}

    return agent


def details_node(model: BaseChatModel):
    extractor = model.bind(response_format=DETAILS_FORMAT)

    async def collect(state: CallState, runtime: Runtime[AgentContext]) -> dict:
        prompt = DETAILS_PROMPT.format(
            caller_phone=state.get("caller_phone") or "unknown",
            details=json.dumps({"accepted": state.get("facts", {}), "pending": state.get("pending_details", {})}),
        )
        # Tool results are unnecessary for extraction and must not supply a caller's identity.
        conversation = [message for message in state["messages"] if isinstance(message, (HumanMessage, AIMessage))]
        conversation = [
            HumanMessage(message.content) if isinstance(message, HumanMessage) else AIMessage(message.text)
            for message in conversation
        ]
        response = await extractor.ainvoke([SystemMessage(prompt), *conversation])
        proposed = read_details(response.text)
        return await asyncio.to_thread(accept_details, state, proposed, runtime.context.maps)

    return collect
