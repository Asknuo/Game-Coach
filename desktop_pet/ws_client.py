"""
WebSocket 客户端 — 在 QThread 中运行，连接 FastAPI Agent。
从原 companion.py 的 TipClient 移植。
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


class TipClient(QThread):
    """在后台线程中通过 WebSocket 接收教练建议。"""

    # 信号：emit 到主线程安全地更新 UI
    tip_received = pyqtSignal(str, str)  # skill, message
    connection_changed = pyqtSignal(bool)  # connected?
    error_occurred = pyqtSignal(str)  # error message

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
                logger.info("Connecting to agent at %s...", WS_URL)
                self._ws = websocket.create_connection(WS_URL, timeout=10)
                self._ws.settimeout(5)
                logger.info("Connected to agent!")
                self.connection_changed.emit(True)

                last_ping = time.time()

                while self._running:
                    try:
                        raw = self._ws.recv()
                        last_ping = time.time()
                    except websocket.WebSocketTimeoutException:
                        pass  # 超时，检查心跳
                    except Exception:
                        raise  # 真正的错误，抛到外层重连
                    else:
                        try:
                            msg = json.loads(raw)
                            if msg.get("type") == "tip":
                                payload = msg.get("payload", {})
                                skill = payload.get("skill", "")
                                message = payload.get("message", "")
                                if message:
                                    self.tip_received.emit(skill, message)
                        except json.JSONDecodeError:
                            pass

                    # 每 15 秒心跳
                    if time.time() - last_ping > 15:
                        try:
                            self._ws.send('{"type":"ping"}')
                            last_ping = time.time()
                        except Exception:
                            break

            except websocket.WebSocketConnectionClosedException:
                logger.info("Connection closed, reconnecting in 3s...")
                self.connection_changed.emit(False)
            except websocket.WebSocketTimeoutException:
                logger.warning("WS timeout, reconnecting in 3s...")
                self.connection_changed.emit(False)
            except Exception as e:
                logger.warning("WS error: %s, reconnecting in 3s...", e)
                self.connection_changed.emit(False)
            finally:
                if self._ws:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

            if self._running:
                time.sleep(3)

    def stop(self):
        """停止线程。"""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self.quit()
        self.wait(3000)
