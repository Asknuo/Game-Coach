"""
采集器桥梁 — 串联 Live Client + LCU，通过 WebSocket 发送给 Agent。

启动方式:
    python -m collector.bridge

或:
    cd agent && python collector/bridge.py

需要先启动 Agent:  python app.py
"""

import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from typing import Optional

# 添加父目录到 path（用于直接运行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bridge")

# ── 检查依赖 ──
try:
    import websocket
except ImportError:
    logger.error("websocket-client 未安装。运行: pip install websocket-client")
    sys.exit(1)

from collector.live_client import LiveClientCollector
from collector.lcu_client import LCUClientCollector

# ── 配置 ──
AGENT_HOST = os.getenv("AGENT_HOST", "localhost")
AGENT_PORT = os.getenv("AGENT_PORT", "8000")
WS_URL = f"ws://{AGENT_HOST}:{AGENT_PORT}/ws/collector"
RECONNECT_DELAY = 3.0
MAX_QUEUE_BACKLOG = 500


class CollectorBridge:
    """连接采集器和 Agent WebSocket 的桥梁。"""

    def __init__(self):
        self._running = True
        self._ws: Optional[websocket.WebSocket] = None
        self._ws_lock = threading.Lock()
        self._send_queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_BACKLOG)

        # 两个采集器
        self.live = LiveClientCollector(callback=self._on_collector_event)
        self.lcu = LCUClientCollector(callback=self._on_collector_event)

        # 状态
        self.live_connected = False
        self.lcu_connected = False
        self.game_active = False

    # ── 采集器回调 ──

    def _on_collector_event(self, event_type: str, payload: dict):
        """接收采集器事件，转为 WS 消息放入发送队列."""
        # 过滤：非游戏内的 LCU 事件，只做日志不发给 Agent
        lcu_info_events = {
            "lcu_connected", "lcu_mastery_loaded", "lcu_runes_updated",
        }

        if event_type == "state":
            # 游戏状态 → 每条都发
            msg = {"type": "state", "payload": payload}
            self._enqueue(msg)

        elif event_type == "event":
            # 游戏事件 (low_health, death, item_purchased, etc.)
            msg = {"type": "event", "payload": payload}
            self._enqueue(msg)

        elif event_type == "live_client_connected":
            self.live_connected = True
            self.game_active = True
            logger.info("游戏对局已连接！开始接收实时数据")

        elif event_type == "live_client_disconnected":
            self.live_connected = False
            self.game_active = False
            logger.info("游戏对局已断开")

        elif event_type == "lcu_connected":
            self.lcu_connected = True
            display = payload.get("summoner", {}).get("displayName", "?")
            logger.info("LCU 已连接 — 召唤师: %s", display)

        elif event_type == "lcu_game_start":
            # LCU 检测到游戏开始 → 发送 context 事件给 Agent
            msg = {"type": "event", "payload": {
                "name": "game_start",
                "data": payload,
            }}
            self._enqueue(msg)
            logger.info("游戏开始上下文已发送: %s", payload.get("summoner_name", "?"))

        elif event_type == "gameflow_phase_change":
            old_phase = payload.get("old_phase", "")
            new_phase = payload.get("new_phase", "")
            logger.info("Gameflow: %s → %s", old_phase, new_phase)

            if new_phase == "EndOfGame":
                self.game_active = False
                logger.info("对局结束")

        elif event_type == "lcu_champion_picked":
            logger.info("已选择英雄: champion_id=%d, position=%s",
                       payload.get("champion_id", 0),
                       payload.get("assigned_position", ""))

        elif event_type not in lcu_info_events:
            # 其他未知事件，只记日志
            logger.debug("未知事件: %s", event_type)

    # ── 发送队列 ──

    def _enqueue(self, msg: dict):
        """将消息放入队列（非阻塞），留给 WS 发送线程处理."""
        try:
            self._send_queue.put_nowait(msg)
        except queue.Full:
            # 队列爆满 → 丢弃最旧的消息
            try:
                self._send_queue.get_nowait()
                self._send_queue.put_nowait(msg)
            except queue.Empty:
                pass

    # ── WebSocket 连接 ──

    def _ws_loop(self):
        """WS 连接 + 发送 + 重连循环."""
        while self._running:
            try:
                logger.info("正在连接 Agent: %s", WS_URL)
                self._ws = websocket.create_connection(WS_URL, timeout=10)
                logger.info("已连接到 Agent!")

                # 启动采集器（连接成功后）
                self.live.start()
                self.lcu.start()

                # 发送循环：从队列取消息 → 发给 Agent
                while self._running:
                    try:
                        msg = self._send_queue.get(timeout=0.5)
                        raw = json.dumps(msg, ensure_ascii=False)
                        with self._ws_lock:
                            if self._ws:
                                try:
                                    self._ws.send(raw)
                                except Exception:
                                    logger.warning("WS 发送失败，准备重连")
                                    break
                    except queue.Empty:
                        # 队列空 → 心跳
                        try:
                            with self._ws_lock:
                                if self._ws:
                                    self._ws.ping()
                        except Exception:
                            logger.warning("心跳失败，准备重连")
                            break

            except (ConnectionRefusedError, OSError, websocket.WebSocketException) as e:
                logger.warning("无法连接 Agent (%s)，%d 秒后重试...", e, RECONNECT_DELAY)
            except Exception as e:
                logger.error("WS 异常: %s", e)
            finally:
                self.live.stop()
                self.lcu.stop()
                with self._ws_lock:
                    if self._ws:
                        try:
                            self._ws.close()
                        except Exception:
                            pass
                        self._ws = None

            if self._running:
                time.sleep(RECONNECT_DELAY)

    # ── 生命周期 ──

    def start(self):
        """启动 Bridge."""
        logger.info("=" * 50)
        logger.info("Collector Bridge 启动")
        logger.info("  Agent:  %s", WS_URL)
        logger.info("  Live Client Data:  游戏中自动连接")
        logger.info("  LCU Client:         大厅中自动连接")
        logger.info("=" * 50)

        self._thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止 Bridge."""
        logger.info("正在关闭 Bridge...")
        self._running = False
        self.live.stop()
        self.lcu.stop()
        with self._ws_lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
        logger.info("Bridge 已停止")

    def wait(self):
        """阻塞主线程直到收到停止信号。"""
        try:
            while self._running:
                time.sleep(1)
                # 定期打印状态
                status_parts = []
                if self.lcu_connected:
                    status_parts.append("LCU:ON")
                else:
                    status_parts.append("LCU:搜索中")
                if self.live_connected:
                    status_parts.append("Live:ON")
                elif self.game_active:
                    status_parts.append("Live:等待游戏")
                else:
                    status_parts.append("Live:空闲")
                status_str = " | ".join(status_parts)
                print(f"\r[{status_str}]", end="", flush=True)
        except KeyboardInterrupt:
            print()
            self.stop()


# ── 入口 ──

def main():
    bridge = CollectorBridge()

    # 信号处理（Ctrl+C）
    def _sigint(sig, frame):
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)

    bridge.start()
    bridge.wait()


if __name__ == "__main__":
    main()
