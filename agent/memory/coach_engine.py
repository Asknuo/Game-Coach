"""对局摘要引擎 — 对局结束后生成摘要，沉淀到 history + facts.

注意: 实时 coaching 生成已迁移至 LangGraph 流水线 (graph/nodes.py)，
本模块只保留对局摘要与 top_of_mind 维护职责。
"""

import json
import logging
import time
import uuid

from memory.injector import MemoryInjector
from memory.models import Fact, PlayerMemory, RecentGame
from models.state import CoachEvent, GameState

logger = logging.getLogger(__name__)


class CoachEngine:
    """对局摘要 + top_of_mind 维护.

    - summarize_game:     对局结束 → 生成对局摘要，更新 history + facts
    - update_top_of_mind: 事件处理时更新 user 层关注点
    """

    def __init__(self, memory: PlayerMemory, injector: MemoryInjector, llm=None):
        self.memory = memory
        self.injector = injector
        self._llm = llm  # 共享的 OpenAIClient（由 app.py 注入，避免重复建客户端）
        self._on_game_saved: object = None  # 对局保存后回调

    # ── top_of_mind 维护 ──────────────────────

    def update_top_of_mind(self, event: CoachEvent, state: GameState | None):
        top = self.memory.user.top_of_mind

        name = event.name
        entry = None
        if name == "dragon_soon" or name == "baron_soon":
            seconds = event.data.get("seconds_left", 0)
            obj = "Dragon" if name == "dragon_soon" else "Baron"
            entry = f"{obj} spawning in {int(seconds)}s"
        elif name == "low_health":
            hp = event.data.get("health_pct", 0)
            entry = f"HPLow({hp:.0f}%)"
        elif name == "item_purchased":
            count = event.data.get("item_count", 0)
            entry = f"Purchased item ({count} total)"

        if entry and entry not in top:
            top.append(entry)
            # 保留最近 6 条
            if len(top) > 6:
                self.memory.user.top_of_mind = top[-6:]

    # ── 对局摘要 ──────────────────────────────

    async def summarize_game(self, session_id: str, state_summary: dict):
        """对局结束后生成摘要，沉淀到 history + facts."""
        llm = self._llm
        if llm is None or not llm._get_async_client():
            # 无 LLM → 用简单的统计摘要
            self._summarize_fallback(state_summary)
            return

        prompt = (
            "Summarize this LoL game into a short structured record. "
            "Output as JSON with keys: champion, result, kills, deaths, assists, "
            "key_moment (one sentence), mistake (one sentence or empty).\n"
            f"Game data: {state_summary}"
        )
        try:
            # ★ 异步链路：不阻塞事件循环
            text = await llm.achat(
                messages=[
                    {"role": "system", "content": "You are a game summarizer. Output JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
                temperature=0.3,
            )
            if not text:
                self._summarize_fallback(state_summary)
                return

            # LLM 可能包裹 ```json ... ``` 代码块，先剥离
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()

            data = json.loads(cleaned)
            game = RecentGame(
                game_id=f"game_{int(time.time())}",
                champion=data.get("champion", ""),
                result=data.get("result", "unknown"),
                kills=data.get("kills", 0),
                deaths=data.get("deaths", 0),
                assists=data.get("assists", 0),
                key_moment=data.get("key_moment", ""),
                mistake=data.get("mistake", ""),
                played_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self.memory.history.recent_games.append(game)

            # 如果 LLM 检测到错误，生成 fact
            if game.mistake:
                fact = Fact(
                    id=f"fact_{uuid.uuid4().hex[:8]}",
                    content=game.mistake,
                    category="behavior",
                    confidence=0.6,
                    created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    source=game.game_id,
                )
                self.memory.facts.append(fact)

            logger.info("game summarized: %s %s", game.champion, game.result)

            # ★ 每局打完立即触发持久化
            if self._on_game_saved:
                self._on_game_saved()
        except Exception:
            logger.exception("LLM summarization failed, using fallback")
            self._summarize_fallback(state_summary)

    def _summarize_fallback(self, summary: dict):
        """无 LLM 时的简单统计摘要 — 也保存对局记录，不丢弃数据."""
        game = RecentGame(
            game_id=f"game_{int(time.time())}",
            champion=summary.get("champion", "unknown"),
            result="unknown",
            kills=summary.get("kills", 0),
            deaths=summary.get("deaths", 0),
            assists=summary.get("assists", 0),
            played_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.memory.history.recent_games.append(game)
        logger.info("game saved (no LLM): %s", game.champion)

        # 同样触发持久化回调
        if self._on_game_saved:
            self._on_game_saved()
