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
    from knowledge.retriever import Retriever

logger = logging.getLogger(__name__)


async def bg_ingest(ctx: AppContext) -> None:
    """后台刷新知识库（摄入是分钟级耗时操作，不阻塞服务启动）.

    复用主进程的 store/embedder：第二个 Chroma 客户端删建 collection 会让
    主进程检索句柄悬空，第二个 embedder 会重复加载 embedding 模型。
    """
    logger.info("Knowledge base stale or missing — refreshing in background...")
    try:
        from knowledge.ingest import Ingestor
        ingestor = Ingestor(store=ctx.retriever.store, embedder=ctx.retriever.embedder)
        await asyncio.to_thread(ingestor.ingest_all)
        logger.info("Knowledge base refresh finished")
    except Exception:
        logger.exception("Auto-refresh knowledge base failed")


def maybe_start_ingest(ctx: AppContext) -> asyncio.Task[None] | None:
    """知识库过期/缺失时启动后台摄入任务，否则返回 None.

    首次启动或超过 7 天未摄入 → 后台自动刷新。
    """
    retriever = ctx.retriever
    if not retriever.available or not retriever.store.needs_refresh():
        return None
    return asyncio.create_task(bg_ingest(ctx))


# 预热英雄数上限：embed LRU 缓存 512 条，留一半给游戏期的动态查询串
_MAX_WARM_CHAMPIONS = 200

# 查询串固定的枚举事件（item_sold 等含物品名，组合空间不可枚举，不预热）
_STATIC_QUERY_EVENTS = (
    "dragon_soon", "baron_soon", "low_health", "item_purchased",
    "kill", "gold_spike", "laning_check", "macro_check",
    "teamfight_detected", "game_end",
)


def _warmup_sync(retriever: Retriever) -> None:
    """同步预热主体（线程池里执行，与 embed_query 复用同一 LRU）."""
    from graph.nodes.routing import _build_rag_query

    champions = retriever.list_champions()[:_MAX_WARM_CHAMPIONS]
    queries: set[str] = set()
    for c in champions:
        queries.add(retriever.matchup_query(c))
        queries.add(retriever.counter_query(c))

    # 固定查询串的事件模板；模板将来新增必需字段时此处会抛错 → 跳过该事件
    for name in _STATIC_QUERY_EVENTS:
        try:
            queries.add(_build_rag_query({"event_name": name, "event_data": {}}))
        except Exception:
            continue

    queries.add("strategy tips priority")  # 未知事件兜底 + 集合探针复用
    retriever.embedder.embed_queries(sorted(queries))
    logger.info(
        "Knowledge warmup: %d champions, %d queries embedded",
        len(champions), len(queries),
    )
    retriever.warm_collections("strategy tips priority", champion=champions[0] if champions else None)


async def bg_warmup(ctx: AppContext, ingest_task: asyncio.Task[None] | None) -> None:
    """启动后台预热：预取英雄/事件查询向量（填 embed LRU）+ 触发 Chroma 索引加载.

    冷启动成本（每局每英雄/事件的首条 tip 付 0.3-1.5s 远程调用 + 索引加载）
    从「游戏中」挪到「进程启动」。失败静默——预热挂了只是回到冷启动行为，
    不影响对局。
    """
    retriever = ctx.retriever
    if not retriever.available:
        logger.info("Knowledge warmup skipped — RAG unavailable")
        return

    # 摄入进行中 → 先等它完成（否则英雄清单读到的是半成品）
    if ingest_task is not None:
        try:
            await ingest_task
        except Exception:
            return  # bg_ingest 已记日志

    try:
        await asyncio.to_thread(_warmup_sync, retriever)
    except Exception:
        logger.exception("Knowledge warmup failed (cold start fallback)")
        return
    logger.info("Knowledge warmup finished")


async def periodic_save(ctx: AppContext) -> None:
    """HA #1: 每 60 秒自动持久化到磁盘，防止进程崩溃丢数据."""
    while True:
        await asyncio.sleep(60)
        try:
            # 写盘挪到工人线程：同步 IO 卡住事件循环会让 WS 收发/队列消费
            # 整体停摆几十 ms（记忆越大越久），造成周期性延迟抖动
            await asyncio.to_thread(ctx.memory_store.save, "default", ctx.memory)
        except Exception:
            logger.exception("Periodic memory save failed")


async def review_on_disconnect(ctx: AppContext, state: GameState) -> None:
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


async def summarize_on_disconnect(
    ctx: AppContext, state: GameState | None, skip_review: bool = False,
) -> None:
    if not state or state.game_time <= 120:
        return

    ap = state.active_player

    # 1. review skill 流水线（LLM 复盘 → overlay 广播）。
    # game_end 事件已走过流水线产出 review tip 时跳过，避免一局复盘两次 LLM 调用
    if not skip_review:
        try:
            await review_on_disconnect(ctx, state)
        except Exception:
            logger.exception("Review on disconnect failed")
    else:
        logger.info("review tip already published via game_end pipeline — skipping")

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
