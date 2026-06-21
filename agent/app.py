import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from graph import build_coaching_graph, set_injections
from graph.nodes import CoachState
from knowledge.chroma_store import ChromaStore
from knowledge.embedder import Embedder
from knowledge.retriever import Retriever, set_retriever
from llm.openai_client import OpenAIClient
from memory.coach_engine import CoachEngine
from memory.injector import MemoryInjector
from memory.models import PlayerMemory
from memory.queue import MemoryQueue
from memory.redis_store import RedisStore
from memory.store import MemoryStore
from models.state import CoachEvent, GameState, WSMessage
from planner.planner import SKILL_REGISTRY, EVENT_TO_SKILL, Planner
from map_zones import get_active_zone, get_enemy_zones

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

# ── 基础组件 ──────────────────────────────────────────
redis_store = RedisStore()
planner = Planner()
llm = OpenAIClient()

# ── 向量知识库 ────────────────────────────────────────
retriever = Retriever(ChromaStore(), Embedder())
set_retriever(retriever)
logger.info(
    "Retriever: %s",
    "available" if retriever.available else "unavailable (set LLM_API_KEY and run ingest)",
)

# ── DeerFlow 风格三级记忆 ────────────────────────────
memory_store = MemoryStore()
memory = memory_store.load("default") or PlayerMemory(session_id="default")
injector = MemoryInjector()

# ── 对局摘要引擎（保留，断开时用） ─────────────────────
engine = CoachEngine(memory, injector)
# ★ 每局打完立即持久化到磁盘（而非等到进程退出）
engine._on_game_saved = lambda: memory_store.save("default", memory)

# ── 防抖队列（LangGraph 的前置过滤层） ─────────────────
queue = MemoryQueue(window=15.0, max_per_window=2, skill_cooldown=25.0)

# ── LangGraph Coaching 图 ────────────────────────────
coaching_graph = build_coaching_graph()
set_injections(
    planner=planner,
    llm=llm,
    retriever=retriever,
    injector=injector,
    redis_store=redis_store,
)
logger.info(
    "LangGraph coaching graph ready: %d nodes",
    len(coaching_graph.nodes) if hasattr(coaching_graph, "nodes") else 8,
)

# ── FastAPI ───────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Game Coach Agent started (LangGraph architecture)")

    # ★ 知识库新鲜度检查：首次启动或超过 7 天未摄入 → 自动刷新
    if retriever.available:
        store = retriever.store
        if store.needs_refresh():
            logger.info("Knowledge base stale or missing — auto-refreshing...")
            try:
                from knowledge.ingest import Ingestor
                Ingestor().ingest_all()
            except Exception:
                logger.exception("Auto-refresh knowledge base failed")

    # ★ HA #1: 每 60 秒自动持久化到磁盘，防止进程崩溃丢数据
    async def periodic_save():
        while True:
            await asyncio.sleep(60)
            try:
                memory_store.save("default", memory)
            except Exception:
                logger.exception("Periodic memory save failed")

    save_task = asyncio.create_task(periodic_save())
    yield
    save_task.cancel()
    memory_store.save("default", memory)
    logger.info("Memory saved to disk")


app = FastAPI(title="Game Coach Agent", lifespan=lifespan)

# ── Overlay WebSocket 广播 ────────────────────────────
overlay_clients: set[WebSocket] = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws/overlay")
async def overlay_ws(websocket: WebSocket):
    """Overlay 专用 WebSocket：接收 tip 并展示+语音播报。"""
    await websocket.accept()
    overlay_clients.add(websocket)
    logger.info("overlay connected (total: %d)", len(overlay_clients))
    try:
        # 保持连接，接收心跳或静音指令
        while True:
            data = await websocket.receive_text()
            # 心跳消息，跳过
            try:
                msg = __import__("json").loads(data)
                if msg.get("type") == "ping":
                    continue
            except Exception:
                pass
            logger.debug("overlay msg: %s", data[:50])
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("overlay ws error")
    finally:
        overlay_clients.discard(websocket)
        logger.info("overlay disconnected (total: %d)", len(overlay_clients))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "game-coach-agent",
        "framework": "langgraph",
        "memory": {
            "facts": len(memory.facts),
            "games": len(memory.history.recent_games),
        },
    }


@app.get("/tips/latest")
async def latest_tips():
    state = redis_store.get_state("default")
    return {
        "state": state,
        "memory": {
            "champion": memory.user.current_champion,
            "phase": memory.user.game_phase,
            "top_of_mind": memory.user.top_of_mind,
            "facts_count": len(memory.facts),
            "games_count": len(memory.history.recent_games),
        },
    }


@app.get("/games")
async def list_games():
    """查询所有历史对局记录（永久保存，不限制数量）."""
    all_games = [g.model_dump() for g in memory.history.recent_games]
    return {
        "total": len(all_games),
        "games": all_games,
        "facts": [f.model_dump() for f in memory.facts],
    }


def _is_urgent(event) -> bool:
    """判断是否紧急事件（绕过防抖队列，立即处理）."""
    name = event.name
    data = event.data
    # 低血量 — 紧急，需要立即回城/回复建议
    if name == "low_health":
        return True
    # 死亡 — 紧急，死亡后需要立即反馈
    if name == "death":
        return True
    # 龙/大龙即将刷新（30秒内）— 紧急预警
    if name in ("dragon_soon", "baron_soon") and data.get("seconds_left", 99) <= 30:
        return True
    return False


@app.websocket("/ws/collector")
async def collector_ws(websocket: WebSocket):
    await websocket.accept()
    latest_state: GameState | None = None
    logger.info("collector connected (langgraph pipeline)")

    # 队列消费回调：事件 → LangGraph → 发送 tip
    async def handle_coaching(item: dict):
        nonlocal latest_state
        event: CoachEvent = item["event"]
        snapshot: GameState | None = item.get("_snapshot") or latest_state
        signals = item.get("signals", [])
        priority = item.get("priority", 1)

        # 构建初始状态，注入 LangGraph
        initial: CoachState = {
            "event": event.model_dump(),
            "game_state": snapshot.model_dump() if snapshot else None,
            "session_id": "default",
            "event_name": "",
            "event_data": {},
            "signals": signals,
            "priority": priority,
            "is_valid": True,
            "skill_name": "",
            "skill_message": "",
            "rag_query": "",
            "rag_docs": [],
            "memory_context": "",
            "polished_message": "",
            "should_publish": False,
            "skip_reason": "",
            "tip": None,
        }

        try:
            result = await coaching_graph.ainvoke(initial)
        except Exception:
            logger.exception("graph.ainvoke failed for %s", event.name)
            return

        tip = result.get("tip")
        if tip:
            response = WSMessage(type="tip", payload=tip)
            tip_json = response.model_dump_json()
            try:
                await websocket.send_text(tip_json)
                logger.info("[%s] %s", tip["skill"], tip["message"][:80])
            except Exception:
                logger.warning("Send tip failed (connection closed)")

            # 同时广播给所有 Overlay 客户端（语音播报）
            dead: list[WebSocket] = []
            for client in overlay_clients:
                try:
                    await client.send_text(tip_json)
                except Exception:
                    dead.append(client)
            for client in dead:
                overlay_clients.discard(client)

            # ★ 反馈闭环：记录发出的建议，供后续检查是否被采纳
            event_name = result.get("event_name", "")
            event_data = result.get("event_data", {})
            redis_store.record_advice_given(
                "default",
                skill=tip["skill"],
                event_name=event_name,
                context={
                    "health_pct": (
                        event_data.get("health_pct", 0) if event_name == "low_health"
                        else 100
                    ),
                    "item_count": len([
                        it for it in (latest_state.active_player.items if latest_state else [])
                        if it.itemID != 0
                    ]),
                },
            )

    queue.set_handler(handle_coaching)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = WSMessage.model_validate_json(raw)

            if msg.type == "state":
                latest_state = GameState.model_validate(msg.payload)
                latest_state.sync_active_player()  # copy items from all_players → active_player
                redis_store.save_state("default", msg.payload)

                # ★ 反馈闭环：检查上次建议是否被采纳
                if latest_state:
                    followed, reason = redis_store.check_advice_followed(
                        "default", msg.payload,
                    )
                    if followed:
                        logger.info("Feedback: advice followed (%s)", reason)
                        redis_store.adjust_skill_confidence("default", "", True)
                    elif reason != "no_advice":
                        logger.debug("Feedback: advice not followed (%s)", reason)
                        redis_store.adjust_skill_confidence("default", "", False)

                # 更新用户上下文
                ap = latest_state.active_player
                memory.user.current_champion = ap.summoner_name
                memory.user.current_gold = ap.current_gold
                memory.user.current_level = ap.level
                if latest_state.game_time < 14 * 60:
                    memory.user.game_phase = "early"
                elif latest_state.game_time < 25 * 60:
                    memory.user.game_phase = "mid"
                else:
                    memory.user.game_phase = "late"

                # ★ 位置区域语义化
                zone = get_active_zone(msg.payload)
                enemy_zones = get_enemy_zones(msg.payload)
                memory.user.context["current_zone"] = zone
                memory.user.context["enemy_zones"] = enemy_zones
                logger.debug("Player zone: %s | Enemies visible: %d", zone, len(enemy_zones))

                continue

            if msg.type == "event":
                event = CoachEvent.model_validate(msg.payload)
                # 入口：死亡时跳过非龙/大龙事件
                if latest_state:
                    hp_pct = latest_state.active_player_health_pct()
                    if hp_pct == 0 and event.name not in ("dragon_soon", "baron_soon"):
                        continue

                # 紧急事件：绕过队列，立即处理
                if _is_urgent(event):
                    logger.info("URGENT event: %s — bypassing queue", event.name)
                    # Capture current state snapshot to avoid race with main loop updates.
                    snapshot = latest_state
                    asyncio.create_task(handle_coaching({
                        "event": event,
                        "signals": [],
                        "priority": 3,
                        "_snapshot": snapshot,
                    }))
                    continue

                # 非紧急事件进入防抖队列
                skill_name = EVENT_TO_SKILL.get(event.name, "")
                meta = SKILL_REGISTRY.get(skill_name, {})
                prio = meta.get("priority", 1)
                if event.name in ("dragon_soon", "baron_soon") and event.data.get("seconds_left", 99) <= 10:
                    prio = 3

                await queue.enqueue({
                    "event": event,
                    "signals": [],
                    "priority": prio,
                })

    except WebSocketDisconnect:
        logger.info("collector disconnected")
        if latest_state and latest_state.game_time > 120:
            # 从 Live Client 事件中提取 KDA
            kills, deaths, assists = 0, 0, 0
            for ev in latest_state.events:
                if ev.event_name == "ChampionKill" and ev.event_time > 0:
                    pass  # 事件层面不分己方/敌方
            summary = {
                "champion": memory.user.current_champion,
                "game_time_s": latest_state.game_time,
                "level": latest_state.active_player.level,
                "gold": latest_state.active_player.current_gold,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
            }
            # ★ 等待摘要完成再继续（确保持久化已完成）
            try:
                await asyncio.wait_for(
                    engine.summarize_game("default", summary), timeout=15.0
                )
            except asyncio.TimeoutError:
                logger.warning("Game summary timed out")
            except Exception:
                logger.exception("Game summary failed")
        memory.user.top_of_mind.clear()
    except Exception:
        logger.exception("websocket error")
        memory_store.save("default", memory)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
