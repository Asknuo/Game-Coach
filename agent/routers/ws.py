"""WebSocket 端点 — /ws/collector（采集双工） /ws/overlay（tip 广播）."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from context import AppContext
from services.session import CollectorSession

logger = logging.getLogger(__name__)

router = APIRouter()


def _ctx(websocket: WebSocket) -> AppContext:
    return websocket.app.state.ctx


@router.websocket("/ws/overlay")
async def overlay_ws(websocket: WebSocket):
    """Overlay 专用 WebSocket：接收 tip 并展示+语音播报."""
    ctx = _ctx(websocket)
    await websocket.accept()
    ctx.overlay_clients.add(websocket)
    logger.info("overlay connected (total: %d)", len(ctx.overlay_clients))
    try:
        # 保持连接，接收心跳
        while True:
            data = await websocket.receive_text()
            # 心跳消息，跳过
            try:
                msg = json.loads(data)
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
        ctx.overlay_clients.discard(websocket)
        logger.info("overlay disconnected (total: %d)", len(ctx.overlay_clients))


@router.websocket("/ws/collector")
async def collector_ws(websocket: WebSocket):
    await websocket.accept()
    ctx = _ctx(websocket)
    # 单飞：多采集器并发会让两个会话交替覆盖同一份 state/记忆
    # （session_id 全程是 "default"）。重连时序靠 collector 的断线
    # 重连保证——旧会话收到 disconnect 才会退出，新连接理应看不到旧会话
    active = getattr(ctx, "collector_session", None)
    if active is not None and not active._ws_closed():
        logger.warning("second collector connection rejected — one active session is enough")
        await websocket.close(code=1008)  # policy violation
        return
    logger.info("collector connected (langgraph pipeline)")
    session = CollectorSession(websocket, ctx)
    ctx.collector_session = session
    try:
        await session.run()
    finally:
        if getattr(ctx, "collector_session", None) is session:
            ctx.collector_session = None
