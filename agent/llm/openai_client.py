import os

from openai import OpenAI

from models.state import CoachingTip, GameState
from prompt.coach_prompt import SYSTEM_PROMPT


class OpenAIClient:
    """LLM 客户端，支持 OpenAI 和 DeepSeek（OpenAI 兼容 API）。

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

    def polish(self, tip: CoachingTip, state: GameState | None,
               rag_context: str | None = None) -> CoachingTip:
        if not self._client:
            return tip

        context = ""
        if state:
            context = (
                f"Game time: {int(state.game_time)}s, "
                f"HP: {state.active_player_health_pct():.0f}%, "
                f"Gold: {int(state.active_player.current_gold)}"
            )

        user_prompt = (
            f"Skill: {tip.skill}\n"
            f"Draft: {tip.message}\n"
            f"Context: {context}\n"
        )
        if rag_context:
            user_prompt += f"Relevant knowledge: {rag_context}\n"
        user_prompt += "Rewrite as one short coaching line (max 20 words)."

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=60,
                temperature=0.7,
            )
            text = response.choices[0].message.content
            if text:
                tip.message = text.strip()
        except Exception:
            pass

        return tip
