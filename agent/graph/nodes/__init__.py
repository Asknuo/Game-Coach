"""LangGraph 节点集合 — 按流水线阶段拆分为 Mixin，GraphNodes 统一组合.

阶段划分:
  parsing    → parse_event / detect_signals（无依赖纯逻辑）
  routing    → route_skill（planner）
  retrieval  → retrieve_knowledge（retriever）
  generation → inject_memory / llm_polish（injector / memory / llm）
  validation → validate / publish（redis_store）
"""

from graph.deps import GraphDeps
from graph.nodes.generation import GenerationMixin
from graph.nodes.parsing import ParsingMixin
from graph.nodes.retrieval import RetrievalMixin
from graph.nodes.routing import RoutingMixin
from graph.nodes.validation import ValidationMixin


class GraphNodes(
    ParsingMixin,
    RoutingMixin,
    RetrievalMixin,
    GenerationMixin,
    ValidationMixin,
):
    """持有依赖的节点集合，build_coaching_graph 将其方法注册为图节点."""

    def __init__(self, deps: GraphDeps):
        self.deps = deps


__all__ = ["GraphNodes"]
