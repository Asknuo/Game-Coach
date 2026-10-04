"""LangGraph 节点依赖 — 显式构造注入，替代原 set_injections 全局单例."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowledge.retriever import Retriever
    from llm.openai_client import OpenAIClient
    from memory.injector import MemoryInjector
    from memory.models import PlayerMemory
    from memory.redis_store import RedisStore
    from models.state import CoachingTip
    from planner.planner import Planner


@dataclass
class GraphDeps:
    """Coaching 图节点的全部外部依赖.

    字段均为可选：测试可只注入关心的依赖（如 FakeRedisStore），
    缺失依赖的节点会自动降级（跳过检索/记忆/去重）。
    """

    planner: Planner | None = None
    llm: OpenAIClient | None = None
    retriever: Retriever | None = None
    injector: MemoryInjector | None = None
    redis_store: RedisStore | None = None
    memory: PlayerMemory | None = None
    # 流式润色增量回调：(tip, 累计文本, tip_id) → 推送 overlay；None = 走非流式 apolish
    on_polish_delta: Callable[[CoachingTip, str, str], Awaitable[None]] | None = None
    # 流式卡片撤回回调：(tip_id, skill) → validate/publish 拒发时通知 overlay 清理
    on_tip_cancel: Callable[[str, str], Awaitable[None]] | None = None
