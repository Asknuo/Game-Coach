"""后台任务与断连收尾 — 知识库摄入 / 周期保存 / 对局复盘."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from graph.state import build_initial_state
from models.state import CoachEvent, GameState, WSMessage
from services import broadcast

if TYPE_CHECKING:
    from context import AppContext

logger = logging.getLogger(__name__)


async def bg_ingest(ctx: "AppContext") -> None:
    """后台刷新知识库（摄入是分钟级耗时操作，不阻塞服务启动）."""
    logger.info("Knowledge base stale or missing — refreshing in background...")
    try:
        from knowledge.ingest import Ingestor
        await asyncio.to_thread(Ingestor().ingest_all)
        logger.info("Knowledge base refresh finished")
    except Exception:
        logger.exception("Auto-refresh knowledge base failed")


def maybe_start_ingest(ctx: "AppContext") -> "asyncio.Task | None":
    """知识库过期/缺失时启动后台摄入任务，否则返回 None.

    首次启动或超过 7 天未摄入 → 后台自动刷新。
    """
    retriever = ctx.retriever
    if not retriever.available or not retriever.store.needs_refresh():
        return None
    return asyncio.create_task(bg_ingest(ctx))


async def periodic_save(ctx: "AppContext") -> None:
    """HA #1: 每 60 秒自动持久化到磁盘，防止进程崩溃丢数据."""
    while True:
        await asyncio.sleep(60)
        try:
            ctx.memory_store.save("default", ctx.memory)
        except Exception:
            logger.exception("Periodic memory save failed")


async def review_on_disconnect(ctx: "AppContext", state: GameState) -> None:
    """断连时生成复盘：走完整 LangGraph 流水线，让 review skill 生效.

    原实现直接调 summarize_game，绕过流水线导致 review skill 永不触发。
    """
    ap = state.active_player
    event = CoachEvent(
        name="game_end",
        data={
            "game_time": state.game_time,
            "champion": ap.champion_name,
            "kills": ap.kills,
            "deaths": ap.deaths,
            "assists": ap.assists,
            "level": ap.level,
            "gold": ap.current_gold,
        },
    )
    initial = build_initial_state(event, state, [], 3)
    try:
        result = await asyncio.wait_for(ctx.coaching_graph.ainvoke(initial), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("Review pipeline timed out")
        return
    except Exception:
        logger.exception("Review pipeline failed")
        return

    tip = result.get("tip")
    if tip:
        tip_json = WSMessage(type="tip", payload=tip).model_dump_json()
        await broadcast.broadcast_tip_json(ctx, tip_json)
        ctx.metrics["tips_published"] += 1
        logger.info("[review] %s", tip["message"][:120])


async def summarize_on_disconnect(ctx: "AppContext", state: GameState | None) -> None:
    if not state or state.game_time <= 120:
        return

    ap = state.active_player

    # 1. review skill 流水线（LLM 复盘 → overlay 广播）
    try:
        await review_on_disconnect(ctx, state)
    except Exception:
        logger.exception("Review on disconnect failed")

    # 2. 结构化摘要沉淀到 history + facts（带真实 KDA）
    summary = {
        "champion": ap.champion_name or ctx.memory.user.current_champion,
        "game_time_s": state.game_time,
        "level": ap.level,
        "gold": ap.current_gold,
        "kills": ap.kills,
        "deaths": ap.deaths,
        "assists": ap.assists,
    }
    try:
        await asyncio.wait_for(ctx.engine.summarize_game("default", summary), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("Game summary timed out")
    except Exception:
        logger.exception("Game summary failed")
