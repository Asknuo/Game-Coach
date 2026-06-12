import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from llm.openai_client import OpenAIClient
from memory.redis_store import RedisStore
from models.state import CoachEvent, GameState, WSMessage
from planner.planner import Planner

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

store = RedisStore()
planner = Planner()
llm = OpenAIClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Game Coach Agent started")
    yield


app = FastAPI(title="Game Coach Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "game-coach-agent"}


@app.websocket("/ws/collector")
async def collector_ws(websocket: WebSocket):
    await websocket.accept()
    session_id = "default"
    latest_state: GameState | None = None
    logger.info("collector connected")

    try:
        while True:
            raw = await websocket.receive_text()
            msg = WSMessage.model_validate_json(raw)

            if msg.type == "state":
                latest_state = GameState.model_validate(msg.payload)
                store.save_state(session_id, msg.payload)
                continue

            if msg.type == "event":
                event = CoachEvent.model_validate(msg.payload)
                tip = planner.plan(event, latest_state)
                if tip is None:
                    continue

                if store.was_tip_recently_sent(session_id, tip.skill):
                    continue

                tip = llm.polish(tip, latest_state)
                store.mark_tip_sent(session_id, tip.skill)

                response = WSMessage(
                    type="tip",
                    payload=tip.model_dump(),
                )
                await websocket.send_text(response.model_dump_json())
                logger.info("[%s] %s", tip.skill, tip.message)

    except WebSocketDisconnect:
        logger.info("collector disconnected")
    except Exception:
        logger.exception("websocket error")


@app.get("/tips/latest")
async def latest_tips():
    """Debug endpoint — returns last saved game state."""
    state = store.get_state("default")
    return {"state": state}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
