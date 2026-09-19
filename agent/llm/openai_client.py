import asyncio
import logging
import os
import time

from openai import AsyncOpenAI

from models.state import CoachingTip, GameState
from prompt.coach_prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


# ── HA #5: 断路器 ──────────────────────────────────

class CircuitBreaker:
    """简单断路器：连续失败 N 次后，冷却 M 秒内拒绝请求，避免打爆 API."""

    def __init__(self, threshold: int = 5, cooldown: float = 60.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._open_until = 0.0

    def is_open(self) -> bool:
        if self._failures >= self.threshold:
            if time.time() < self._open_until:
                return True  # 断路器打开，拒绝请求
            self._failures = 0  # 冷却结束，重置
        return False

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.threshold:
            self._open_until = time.time() + self.cooldown

    def record_success(self):
        self._failures = 0


class OpenAIClient:
    """LLM 客户端，支持 OpenAI 和 DeepSeek（OpenAI 兼容 API）。

    全异步（apolish / achat）：主流水线运行在 FastAPI 事件循环内，
    同步通道无任何调用方，已随 P1 清理删除。

    通过环境变量切换：
    - LLM_API_KEY → API Key
    - LLM_BASE_URL → 默认 https://api.deepseek.com/v1
    - LLM_MODEL    → 默认 deepseek-chat
    """

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        self.api_key = (
            api_key
            or os.getenv("LLM_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")  # fallback 旧变量
        )
        self.model = (
            model
            or os.getenv("LLM_MODEL", "deepseek-chat")
        )
        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        )
        # 客户端级超时：SDK 默认 600s 会让一次挂起停摆整条流水线
        self._timeout = float(os.getenv("LLM_TIMEOUT", "20"))

        self._aclient: AsyncOpenAI | None = None

        # ★ HA #5: 断路器，连续 5 次失败后冷却 60 秒
        self._breaker = CircuitBreaker(threshold=5, cooldown=60.0)

    # ── 异步客户端（懒加载） ──

    def _get_async_client(self) -> AsyncOpenAI | None:
        if not self.api_key:
            return None
        if self._aclient is None:
            self._aclient = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self._timeout,
                max_retries=0,
            )
        return self._aclient

    # ── 异步链路（不阻塞事件循环） ──

    async def achat(self, messages: list, max_tokens: int,
                    temperature: float = 0.7) -> str | None:
        """异步调用 LLM，带断路器 + 指数退避重试（最多 3 次）."""
        aclient = self._get_async_client()
        if not aclient or self._breaker.is_open():
            return None

        delay = 1.0
        for attempt in range(3):
            try:
                resp = await aclient.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if resp and resp.choices:
                    text = resp.choices[0].message.content
                    if text:
                        self._breaker.record_success()
                        return text.strip()
                return None
            except Exception:
                logger.debug("achat attempt %d failed", attempt + 1, exc_info=True)
                if attempt < 2:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10)

        self._breaker.record_failure()
        return None

    # ── Prompt 构建（同步/异步共用） ──

    @staticmethod
    def _build_polish_prompt(tip: CoachingTip, state: GameState | None,
                             rag_context: str | None) -> tuple[str, int]:
        context = ""
        if state:
            context = (
                f"Game time: {int(state.game_time)}s, "
                f"HP: {state.active_player_health_pct():.0f}%, "
                f"Gold: {int(state.active_player.current_gold)}"
            )

        is_rich = rag_context and len(rag_context) > 200

        user_prompt = (
            f"Skill: {tip.skill}\n"
            f"Draft: {tip.message}\n"
            f"Context: {context}\n"
        )
        if rag_context:
            user_prompt += f"Relevant knowledge: {rag_context}\n"

        if is_rich:
            user_prompt += "Synthesize into 2-3 short actionable coaching sentences. Prioritize matchup-specific insights."
            max_tokens = 120
        else:
            user_prompt += "Rewrite as one short coaching line (max 20 words)."
            max_tokens = 60

        return user_prompt, max_tokens

    # ── 润色：异步版 ──

    async def apolish(self, tip: CoachingTip, state: GameState | None,
                      rag_context: str | None = None) -> CoachingTip:
        if not self._get_async_client() or self._breaker.is_open():
            return tip

        user_prompt, max_tokens = self._build_polish_prompt(tip, state, rag_context)
        text = await self.achat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
        )
        if text:
            tip.message = text
        return tip
