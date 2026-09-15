"""The agent graph: START → agent ⇄ tools → END. One run per caller utterance."""

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from voice_agent.agent.agent import TOOLS, agent_node
from voice_agent.agent.state import AgentContext, CallState


def after_tools(state: CallState) -> str:
    """Stop once end_call ran; otherwise let the agent read the tool results."""
    return END if state.get("call_finished") else "agent"


def build_graph(model: BaseChatModel, checkpointer: BaseCheckpointSaver | None = None):
    graph = StateGraph(CallState, context_schema=AgentContext)
    graph.add_node("agent", agent_node(model))
    graph.add_node("tools", ToolNode(TOOLS))

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, ["tools", END])
    graph.add_conditional_edges("tools", after_tools, ["agent", END])

    return graph.compile(checkpointer=checkpointer)
