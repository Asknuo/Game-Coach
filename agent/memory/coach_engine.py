"""LLM 驱动的 coaching 建议生成 + 记忆更新引擎."""

import asyncio
import logging
import time
import uuid

from memory.models import Fact, PlayerMemory, RecentGame
from memory.injector import MemoryInjector
from models.state import CoachEvent, CoachingTip, GameState

logger = logging.getLogger(__name__)


class CoachEngine:
    """调用 LLM 生成 coaching 建议，同时异步更新三级记忆.

    核心方法:
    - generate:  给定事件 + 状态 → 返回 CoachingTip
    - summarize: 对局结束 → 生成对局摘要，更新 history + facts
    """

    def __init__(self, memory: PlayerMemory, injector: MemoryInjector):
        self.memory = memory
        self.injector = injector
        self._on_game_saved: object = None  # 对局保存后回调

    # ── Coaching 生成 ──────────────────────────

    async def generate(self, item: dict, state: GameState | None) -> CoachingTip | None:
        """从经过预处理的事件生成 coaching 建议.

        流程:
        1. 注入记忆上下文
        2. 委托给 planner + skills 生成模板建议
        3. 更新 user 层 top_of_mind
        """
        event: CoachEvent = item["event"]
        signals: list[str] = item.get("signals", [])

        # 步骤 1+2: 用现有的 planner → skills 链路（保留 RAG 增强）
        from planner.planner import Planner

        planner = Planner()
        tip = planner.plan(event, state)
        if tip is None:
            return None

        # 步骤 3: 更新 top_of_mind
        self._update_top_of_mind(event, state)

        # 步骤 4: LLM 润色（如果有 rag_context，polish 会自行处理）
        from llm.openai_client import OpenAIClient

        llm = OpenAIClient()
        memory_context = self.injector.format(self.memory, token_budget=200)
        tip = llm.polish(tip, state, rag_context=memory_context if memory_context else None)

        return tip

    # ── 记忆更新 ──────────────────────────────

    def _update_top_of_mind(self, event: CoachEvent, state: GameState | None):
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
        from llm.openai_client import OpenAIClient

        llm = OpenAIClient()
        if not llm._client:
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
            resp = llm._client.chat.completions.create(
                model=llm.model,
                messages=[
                    {"role": "system", "content": "You are a game summarizer. Output JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
                temperature=0.3,
            )
            text = resp.choices[0].message.content
            if text:
                import json
                data = json.loads(text)
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
