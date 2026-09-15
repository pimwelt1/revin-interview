"""Tools that control the phone call itself."""

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from voice_agent.agent.state import AgentContext, CallState


@tool
def end_call(runtime: ToolRuntime[AgentContext, CallState]) -> Command:
    """End the phone call once the caller has nothing else. The phone system says goodbye."""
    return Command(update={"call_finished": True, "messages": [ToolMessage("Call ending.", tool_call_id=runtime.tool_call_id)]})
