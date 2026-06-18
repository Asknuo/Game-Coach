"""LangGraph AgentState — coaching 流水线中流转的状态."""

from typing import Any, NotRequired, Optional

from typing_extensions import TypedDict


class CoachState(TypedDict):
    """LangGraph 状态，在节点间单向流转。

    字段分为三个阶段:
      input    → parse_event 填入
      planning → route_skill / retrieve / inject 填入
      output   → validate / publish 填入
    """

    # ── Input（外部注入） ──
    event: dict[str, Any]              # CoachEvent.model_dump()
    game_state: dict[str, Any] | None  # GameState.model_dump() or None
    session_id: str

    # ── Parsing（parse_event / detect_signals） ──
    event_name: str
    event_data: dict[str, Any]
    signals: list[str]
    priority: int
    is_valid: bool                     # False → 直接 END

    # ── Planning（route_skill） ──
    skill_name: str
    skill_message: str                 # Planner.plan() 基础消息
    rag_query: str                     # 向量检索查询文本
    skill_context: str                 # SKILL.md 正文（coaching 指导方针）
    skill_gotchas: str                 # gotchas.md 坑点清单

    # ── Retrieval（retrieve_knowledge） ──
    rag_docs: list[str]                # ChromaDB 检索结果摘要

    # ── Memory（inject_memory） ──
    memory_context: str                # PlayerMemory 格式化文本

    # ── Generation（llm_polish） ──
    polished_message: str              # LLM 润色后文本

    # ── Validation（validate） ──
    should_publish: bool               # True = 发送, False = 跳过
    skip_reason: str                   # "", "duplicate", "invalid", "no_llm"

    # ── Output ──
    tip: dict[str, Any] | None         # CoachingTip.model_dump(), None 表示不发送
