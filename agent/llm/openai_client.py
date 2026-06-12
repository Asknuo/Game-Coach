import os

from openai import OpenAI

from models.state import CoachingTip, GameState
from prompt.coach_prompt import SYSTEM_PROMPT


class OpenAIClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self._client = OpenAI(api_key=self.api_key) if self.api_key else None

    def polish(self, tip: CoachingTip, state: GameState | None) -> CoachingTip:
        if not self._client:
            return tip

        context = ""
        if state:
            context = (
                f"Game time: {int(state.game_time)}s, "
                f"HP: {state.active_player_health_pct():.0f}%, "
                f"Gold: {int(state.active_player.current_gold)}"
            )

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Skill: {tip.skill}\n"
                            f"Draft: {tip.message}\n"
                            f"Context: {context}\n"
                            "Rewrite as one short coaching line (max 20 words)."
                        ),
                    },
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
