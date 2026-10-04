"""应用上下文 — 所有共享组件的装配与持有.

AppContext 集中持有运行期单例（Redis / 记忆 / LLM / LangGraph），
通过 app.state.ctx 传递给 routers 和 services，替代原 app.py 的模块级全局变量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from graph import GraphDeps, build_coaching_graph
from knowledge.chroma_store import ChromaStore
from knowledge.embedder import Embedder
from knowledge.retriever import Retriever
from llm.openai_client import OpenAIClient
from memory.coach_engine import CoachEngine
from memory.injector import MemoryInjector
from memory.models import PlayerMemory
from memory.queue import MemoryQueue
from memory.redis_store import RedisStore
from memory.store import MemoryStore
from models.state import WSMessage
from planner.planner import Planner
from services import broadcast


def _default_metrics() -> dict[str, int | float]:
    """轻量运行时指标（/health 暴露）."""
    return {
        "events_received": 0,
        "events_urgent": 0,
        "events_queued": 0,
        "tips_published": 0,
        "tips_skipped": 0,
        "tips_dropped": 0,
        "urgent_dropped": 0,
        "session_aborts": 0,
        "session_resets": 0,
        "graph_errors": 0,
        "advice_followed": 0,
        "advice_expired": 0,
        # skip 原因分维度计数（validate/publish 拒发时按 skip_reason 累加）
        "skipped_duplicate": 0,
        "skipped_low_confidence": 0,
        "skipped_stale": 0,
        "skipped_other": 0,
        # tip 延迟累计（秒）：除以 tips_published 得均值
        "latency_total_s": 0.0,
        "latency_pipeline_s": 0.0,
    }


@dataclass
class AppContext:
    """运行期共享组件容器."""

    redis_store: RedisStore
    planner: Planner
    llm: OpenAIClient
    retriever: Retriever
    memory_store: MemoryStore
    memory: PlayerMemory
    injector: MemoryInjector
    engine: CoachEngine
    queue: MemoryQueue
    coaching_graph: Any
    stream_broadcaster: Any = None  # PolishStreamBroadcaster（lifespan 生命周期管理）
    collector_session: Any = None    # 活跃的 collector 会话（单飞保护）
    metrics: dict[str, int | float] = field(default_factory=_default_metrics)
    overlay_clients: set[WebSocket] = field(default_factory=set)


def build_context() -> AppContext:
    """装配全部组件（app.py 导入期执行一次）."""
    # ── 基础组件 ──
    redis_store = RedisStore()
    planner = Planner()
    llm = OpenAIClient()

    # ── 向量知识库 ──
    retriever = Retriever(ChromaStore(), Embedder())

    # ── DeerFlow 风格三级记忆 ──
    memory_store = MemoryStore()
    memory = memory_store.load("default") or PlayerMemory(session_id="default")
    injector = MemoryInjector()

    # ── 对局摘要引擎（对局结束/断开时用） ──
    engine = CoachEngine(memory, injector, llm=llm)
    # 每局打完立即持久化到磁盘（而非等到进程退出）
    engine._on_game_saved = lambda: memory_store.save("default", memory)

    # ── 防抖队列（LangGraph 的前置过滤层） ──
    queue = MemoryQueue(window=6.0, max_per_window=2, skill_cooldown=25.0, burst_flush_at=3)

    # ── LangGraph Coaching 图（显式依赖注入） ──
    # emitter 闭包引用 ctx，而 graph 又要放进 ctx —— 先占位建 ctx，再回填 graph
    ctx = AppContext(
        redis_store=redis_store,
        planner=planner,
        llm=llm,
        retriever=retriever,
        memory_store=memory_store,
        memory=memory,
        injector=injector,
        engine=engine,
        queue=queue,
        coaching_graph=None,
    )
    # 队列把超额丢弃计入 /health 指标（截断丢弃此前完全不可见）
    queue.metrics = ctx.metrics

    # 流式增量合并广播：emit 只写内存，worker 统一发送，LLM 迭代不等网络 IO
    stream_broadcaster = broadcast.PolishStreamBroadcaster(ctx)
    ctx.stream_broadcaster = stream_broadcaster

    async def _emit_polish_delta(tip, text: str, tip_id: str) -> None:
        """流式润色增量 → overlay（type=tip_stream，客户端按 tip_id 归属卡片）."""
        payload = {"skill": tip.skill, "priority": tip.priority,
                   "message": text, "tip_id": tip_id}
        await stream_broadcaster.emit(payload, tip_id)

    async def _emit_tip_cancel(tip_id: str, skill: str) -> None:
        """validate/publish 拒发 → 通知 overlay 撤回落单的流式卡片."""
        await broadcast.broadcast_tip_json(
            ctx,
            WSMessage(type="tip_cancel",
                      payload={"tip_id": tip_id, "skill": skill}).model_dump_json(),
        )

    ctx.coaching_graph = build_coaching_graph(GraphDeps(
        planner=planner,
        llm=llm,
        retriever=retriever,
        injector=injector,
        redis_store=redis_store,
        memory=memory,
        on_polish_delta=_emit_polish_delta,
        on_tip_cancel=_emit_tip_cancel,
    ))

    return ctx
