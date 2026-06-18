import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
    yield
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

# 挂载静态文件目录
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/overlay")
async def overlay():
    """浏览器 Overlay 页面（内置语音）。"""
    overlay_path = os.path.join(static_dir, "overlay.html")
    if os.path.exists(overlay_path):
        return FileResponse(overlay_path)
    return {"error": "overlay.html not found, run setup first"}


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
            # 可扩展：mute/unmute 等指令
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


@app.websocket("/ws/collector")
async def collector_ws(websocket: WebSocket):
    await websocket.accept()
    latest_state: GameState | None = None
    logger.info("collector connected (langgraph pipeline)")

    # 队列消费回调：事件 → LangGraph → 发送 tip
    async def handle_coaching(item: dict):
        nonlocal latest_state

        event = item["event"]
        signals = item.get("signals", [])
        priority = item.get("priority", 1)

        # 构建初始状态，注入 LangGraph
        initial: CoachState = {
            "event": event.model_dump(),
            "game_state": latest_state.model_dump() if latest_state else None,
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

    queue.set_handler(handle_coaching)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = WSMessage.model_validate_json(raw)

            if msg.type == "state":
                latest_state = GameState.model_validate(msg.payload)
                redis_store.save_state("default", msg.payload)

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
                continue

            if msg.type == "event":
                event = CoachEvent.model_validate(msg.payload)
                # 入口：预处理 → 入队
                # 死亡时跳过低优先级事件
                if latest_state:
                    hp_pct = latest_state.active_player_health_pct()
                    if hp_pct == 0 and event.name not in ("dragon_soon", "baron_soon"):
                        continue

                # 优先级（从 SKILL_REGISTRY 获取）
                skill_name = EVENT_TO_SKILL.get(event.name, "")
                meta = SKILL_REGISTRY.get(skill_name, {})
                prio = meta.get("priority", 1)
                if event.name in ("dragon_soon", "baron_soon") and event.data.get("seconds_left", 99) <= 10:
                    prio = 3  # 即将刷新升级为高优先级

                await queue.enqueue({
                    "event": event,
                    "signals": [],
                    "priority": prio,
                })

    except WebSocketDisconnect:
        logger.info("collector disconnected")
        if latest_state and latest_state.game_time > 120:
            summary = {
                "champion": memory.user.current_champion,
                "game_time_s": latest_state.game_time,
                "level": latest_state.active_player.level,
                "gold": latest_state.active_player.current_gold,
            }
            asyncio.create_task(engine.summarize_game("default", summary))
        memory_store.save("default", memory)
        memory.user.top_of_mind.clear()
    except Exception:
        logger.exception("websocket error")
        memory_store.save("default", memory)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
