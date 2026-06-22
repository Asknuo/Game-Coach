"""
WebSocket 客户端 — 在 QThread 中运行，连接 FastAPI Agent。
连接 FastAPI Agent 的 /ws/overlay 端点，接收 coaching tip。
"""

import json
import logging
import os
import time

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger("desktop_pet.ws")

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

AGENT_HOST = os.getenv("AGENT_HOST", "localhost")
AGENT_PORT = os.getenv("AGENT_PORT", "8000")
WS_URL = f"ws://{AGENT_HOST}:{AGENT_PORT}/ws/overlay"

PING_INTERVAL_SEC = 15
RECONNECT_DELAY_SEC = 3


class TipClient(QThread):
    """在后台线程中通过 WebSocket 接收教练建议。"""

    tip_received = pyqtSignal(str, str)  # skill, message
    connection_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._ws = None
        if not HAS_WS:
            logger.error(
                "websocket-client not installed. Run: pip install websocket-client"
            )

    def run(self):
        """线程主循环：连接 → 接收 → 重连。"""
        if not HAS_WS:
            self.error_occurred.emit("websocket-client 未安装")
            return

        while self._running:
            try:
                self._connect_once()
            except websocket.WebSocketConnectionClosedException as exc:
                self._log_disconnect(exc)
            except websocket.WebSocketTimeoutException as exc:
                self._log_disconnect(exc)
            except Exception as exc:
                self._log_disconnect(exc)
            finally:
                self._close_ws()

            if self._running:
                time.sleep(RECONNECT_DELAY_SEC)

    def _connect_once(self) -> None:
        logger.info("Connecting to agent at %s...", WS_URL)
        self._ws = websocket.create_connection(WS_URL, timeout=10)
        self._ws.settimeout(5)
        logger.info("Connected to agent!")
        self.connection_changed.emit(True)
        self._session_loop()

    def _session_loop(self) -> None:
        last_ping = time.time()
        while self._running:
            raw = self._recv_or_timeout()
            if raw is not None:
                last_ping = time.time()
                self._emit_tip_from_raw(raw)

            if time.time() - last_ping <= PING_INTERVAL_SEC:
                continue
            try:
                self._ws.send('{"type":"ping"}')
                last_ping = time.time()
            except Exception:
                break

    def _recv_or_timeout(self) -> str | None:
        try:
            return self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None

    def _emit_tip_from_raw(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("type") != "tip":
            return
        payload = msg.get("payload", {})
        message = payload.get("message", "")
        if message:
            self.tip_received.emit(payload.get("skill", ""), message)

    def _log_disconnect(self, exc: Exception) -> None:
        if isinstance(exc, websocket.WebSocketConnectionClosedException):
            logger.info("Connection closed, reconnecting in %ds...", RECONNECT_DELAY_SEC)
        elif isinstance(exc, websocket.WebSocketTimeoutException):
            logger.warning("WS timeout, reconnecting in %ds...", RECONNECT_DELAY_SEC)
        else:
            logger.warning("WS error: %s, reconnecting in %ds...", exc, RECONNECT_DELAY_SEC)
        self.connection_changed.emit(False)

    def _close_ws(self) -> None:
        if not self._ws:
            return
        try:
            self._ws.close()
        except OSError:
            pass
        self._ws = None

    def stop(self):
        """停止线程。"""
        self._running = False
        self._close_ws()
        self.quit()
        self.wait(3000)
