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

# ── FastAPI ───────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    node_count = len(coaching_graph.nodes) if hasattr(coaching_graph, "nodes") else 0
    rag_status = "on" if retriever.available else "off"
    logger.info(
        "Game Coach Agent ready — skills=%d, nodes=%d, rag=%s",
        len(SKILL_REGISTRY),
        node_count,
        rag_status,
    )
    if not retriever.available:
        logger.warning("RAG unavailable — set LLM_API_KEY and run: python -m knowledge.ingest")

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


def _is_urgent(event: CoachEvent) -> bool:
    """判断是否紧急事件（绕过防抖队列，立即处理）."""
    name = event.name
    data = event.data
    if name in ("low_health", "death"):
        return True
    return name in ("dragon_soon", "baron_soon") and data.get("seconds_left", 99) <= 30


def _game_phase(game_time: float) -> str:
    if game_time < 14 * 60:
        return "early"
    if game_time < 25 * 60:
        return "mid"
    return "late"


def _event_priority(event: CoachEvent) -> int:
    skill_name = EVENT_TO_SKILL.get(event.name, "")
    meta = SKILL_REGISTRY.get(skill_name, {})
    prio = meta.get("priority", 1)
    if event.name in ("dragon_soon", "baron_soon") and event.data.get("seconds_left", 99) <= 10:
        return 3
    return prio


def _build_coach_state(
    event: CoachEvent,
    snapshot: GameState | None,
    signals: list,
    priority: int,
) -> CoachState:
    return {
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


async def _broadcast_tip_json(tip_json: str) -> None:
    dead: list[WebSocket] = []
    for client in overlay_clients:
        try:
            await client.send_text(tip_json)
        except Exception:
            dead.append(client)
    for client in dead:
        overlay_clients.discard(client)


def _record_advice_context(
    tip: dict,
    result: dict,
    latest_state: GameState | None,
) -> None:
    event_name = result.get("event_name", "")
    event_data = result.get("event_data", {})
    items = latest_state.active_player.items if latest_state else []
    item_count = len([it for it in items if it.itemID != 0])
    redis_store.record_advice_given(
        "default",
        skill=tip["skill"],
        event_name=event_name,
        context={
            "health_pct": event_data.get("health_pct", 0) if event_name == "low_health" else 100,
            "item_count": item_count,
        },
    )


def _update_memory_from_state(state: GameState, payload: dict) -> None:
    ap = state.active_player
    memory.user.current_champion = ap.summoner_name
    memory.user.current_gold = ap.current_gold
    memory.user.current_level = ap.level
    memory.user.game_phase = _game_phase(state.game_time)
    memory.user.context["current_zone"] = get_active_zone(payload)
    memory.user.context["enemy_zones"] = get_enemy_zones(payload)


def _check_advice_feedback(payload: dict) -> None:
    followed, reason = redis_store.check_advice_followed("default", payload)
    if followed:
        logger.info("Feedback: advice followed (%s)", reason)
        redis_store.adjust_skill_confidence("default", "", True)
    elif reason != "no_advice":
        logger.debug("Feedback: advice not followed (%s)", reason)
        redis_store.adjust_skill_confidence("default", "", False)


def _should_skip_dead_event(state: GameState | None, event: CoachEvent) -> bool:
    if not state:
        return False
    if event.name in ("dragon_soon", "baron_soon"):
        return False
    return state.active_player_health_pct() == 0


async def _summarize_on_disconnect(state: GameState | None) -> None:
    if not state or state.game_time <= 120:
        return
    summary = {
        "champion": memory.user.current_champion,
        "game_time_s": state.game_time,
        "level": state.active_player.level,
        "gold": state.active_player.current_gold,
        "kills": 0,
        "deaths": 0,
        "assists": 0,
    }
    try:
        await asyncio.wait_for(engine.summarize_game("default", summary), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("Game summary timed out")
    except Exception:
        logger.exception("Game summary failed")


class CollectorSession:
    """Collector WebSocket 会话：state/event 分发 + LangGraph 流水线。"""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.latest_state: GameState | None = None
        self._background_tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        queue.set_handler(self.handle_coaching)
        try:
            while True:
                raw = await self.websocket.receive_text()
                msg = WSMessage.model_validate_json(raw)
                if msg.type == "state":
                    self._on_state(msg)
                elif msg.type == "event":
                    await self._on_event(msg)
        except WebSocketDisconnect:
            logger.info("collector disconnected")
            await _summarize_on_disconnect(self.latest_state)
            memory.user.top_of_mind.clear()
        except Exception:
            logger.exception("websocket error")
            memory_store.save("default", memory)

    async def handle_coaching(self, item: dict) -> None:
        event: CoachEvent = item["event"]
        snapshot: GameState | None = item.get("_snapshot") or self.latest_state
        initial = _build_coach_state(
            event, snapshot, item.get("signals", []), item.get("priority", 1),
        )
        try:
            result = await coaching_graph.ainvoke(initial)
        except Exception:
            logger.exception("graph.ainvoke failed for %s", event.name)
            return

        tip = result.get("tip")
        if not tip:
            return

        tip_json = WSMessage(type="tip", payload=tip).model_dump_json()
        try:
            await self.websocket.send_text(tip_json)
            logger.info("[%s] %s", tip["skill"], tip["message"][:80])
        except Exception:
            logger.warning("Send tip failed (connection closed)")

        await _broadcast_tip_json(tip_json)
        _record_advice_context(tip, result, self.latest_state)

    def _on_state(self, msg: WSMessage) -> None:
        self.latest_state = GameState.model_validate(msg.payload)
        self.latest_state.sync_active_player()
        redis_store.save_state("default", msg.payload)
        _check_advice_feedback(msg.payload)
        _update_memory_from_state(self.latest_state, msg.payload)
        zone = memory.user.context.get("current_zone", "")
        enemies = memory.user.context.get("enemy_zones", [])
        logger.debug("Player zone: %s | Enemies visible: %d", zone, len(enemies))

    async def _on_event(self, msg: WSMessage) -> None:
        event = CoachEvent.model_validate(msg.payload)
        if _should_skip_dead_event(self.latest_state, event):
            return

        if _is_urgent(event):
            logger.info("URGENT event: %s — bypassing queue", event.name)
            self._spawn_coaching({
                "event": event,
                "signals": [],
                "priority": 3,
                "_snapshot": self.latest_state,
            })
            return

        await queue.enqueue({
            "event": event,
            "signals": [],
            "priority": _event_priority(event),
        })

    def _spawn_coaching(self, item: dict) -> None:
        task = asyncio.create_task(self.handle_coaching(item))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


@app.websocket("/ws/collector")
async def collector_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("collector connected (langgraph pipeline)")
    await CollectorSession(websocket).run()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run("app:app", host=host, port=port, reload=True)
