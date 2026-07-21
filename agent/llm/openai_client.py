import asyncio
import logging
import os
import time

from openai import AsyncOpenAI, OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

    同步 (polish) 与异步 (apolish / achat) 双通道：
    - LangGraph 同步节点 / 离线脚本 → polish
    - async 上下文（FastAPI 事件循环、CoachEngine） → apolish / achat
      （不会阻塞事件循环）

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

        if self.api_key:
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        else:
            self._client = None
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
            )
        return self._aclient

    # ── 同步链路（LangGraph 同步节点用） ──

    # ★ HA #5: 指数退避重试（1s → 2s → 4s，最多 3 次）
    def _call_with_retry(self, messages: list, max_tokens: int,
                         temperature: float = 0.7):

        @retry(
            retry=retry_if_exception_type(Exception),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=False,
        )
        def _do_call():
            return self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        return _do_call()

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

    # ── 润色：同步版 ──

    def polish(self, tip: CoachingTip, state: GameState | None,
               rag_context: str | None = None) -> CoachingTip:
        if not self._client:
            return tip

        # ★ HA #5: 断路器打开 → 直接跳过 LLM
        if self._breaker.is_open():
            return tip

        user_prompt, max_tokens = self._build_polish_prompt(tip, state, rag_context)

        try:
            response = self._call_with_retry(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
            )
            if response and response.choices:
                text = response.choices[0].message.content
                if text:
                    tip.message = text.strip()
                    self._breaker.record_success()
        except Exception:
            self._breaker.record_failure()

        return tip
