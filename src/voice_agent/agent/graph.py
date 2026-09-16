"""The agent graph: START → details → agent ⇄ tools → END. One run per caller utterance."""

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from voice_agent.agent.agent import TOOLS, agent_node, build_model, details_node
from voice_agent.agent.state import AgentContext, CallState
from voice_agent.settings import Settings


def after_tools(state: CallState) -> str:
    """Stop once end_call ran; otherwise let the agent read the tool results."""
    return END if state.get("call_finished") else "agent"


def build_call_graph(settings: Settings):
    """The graph every entry point runs, with the transcript kept in memory only."""
    return build_graph(build_model(settings), InMemorySaver())


def build_graph(model: BaseChatModel, checkpointer: BaseCheckpointSaver | None = None):
    # The two model nodes bind the same model differently: tools for the agent, a JSON schema for extraction.
    graph = StateGraph(CallState, context_schema=AgentContext)
    graph.add_node("details", details_node(model))
    graph.add_node("agent", agent_node(model))
    graph.add_node("tools", ToolNode(TOOLS))

    graph.add_edge(START, "details")
    graph.add_edge("details", "agent")
    graph.add_conditional_edges("agent", tools_condition, ["tools", END])
    graph.add_conditional_edges("tools", after_tools, ["agent", END])

    return graph.compile(checkpointer=checkpointer)
