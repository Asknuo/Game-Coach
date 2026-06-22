"""LangGraph StateGraph 构建器 — coaching 决策图."""

import logging

from langgraph.graph import END, StateGraph

from graph.nodes import (
    detect_signals,
    inject_memory,
    llm_polish,
    parse_event,
    publish,
    retrieve_knowledge,
    route_skill,
    validate,
)
from graph.state import CoachState

logger = logging.getLogger(__name__)


def _should_continue(state: CoachState) -> str:
    """parse_event 后的条件路由."""
    if not state.get("is_valid"):
        logger.debug("route: invalid event → END")
        return END
    return "detect_signals"


def _after_skill(state: CoachState) -> str:
    """route_skill 后的条件路由."""
    if not state.get("is_valid"):
        logger.debug("route: no skill → END")
        return END
    return "retrieve_knowledge"


def _after_validate(state: CoachState) -> str:
    """validate 后的条件路由."""
    if state.get("should_publish"):
        return "publish"
    logger.debug("route: skip (reason=%s) → END", state.get("skip_reason", "?"))
    return END


def build_coaching_graph() -> StateGraph:
    """构建并编译 Coaching Agent 状态图.

    Flow:
      parse_event
        ├─ invalid → END
        └─ valid → detect_signals
                      │
                      ▼
                   route_skill
                      ├─ no skill → END
                      └─ has skill → retrieve_knowledge
                                       │
                                       ▼
                                   inject_memory
                                       │
                                       ▼
                                   llm_polish
                                       │
                                       ▼
                                   validate
                                      ├─ skip → END
                                      └─ publish → END
    """
    builder = StateGraph(CoachState)

    # 节点注册
    builder.add_node("parse_event", parse_event)
    builder.add_node("detect_signals", detect_signals)
    builder.add_node("route_skill", route_skill)
    builder.add_node("retrieve_knowledge", retrieve_knowledge)
    builder.add_node("inject_memory", inject_memory)
    builder.add_node("llm_polish", llm_polish)
    builder.add_node("validate", validate)
    builder.add_node("publish", publish)

    # 边
    builder.set_entry_point("parse_event")
    builder.add_conditional_edges("parse_event", _should_continue)
    builder.add_edge("detect_signals", "route_skill")
    builder.add_conditional_edges("route_skill", _after_skill)
    builder.add_edge("retrieve_knowledge", "inject_memory")
    builder.add_edge("inject_memory", "llm_polish")
    builder.add_edge("llm_polish", "validate")
    builder.add_conditional_edges("validate", _after_validate)
    builder.add_edge("publish", END)

    graph = builder.compile()
    logger.debug("Coaching graph compiled: %d nodes", len(graph.nodes))
    return graph
