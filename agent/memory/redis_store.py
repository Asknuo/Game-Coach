import json
import os
from typing import Any

import redis


class RedisStore:
    """Short-term memory for the current game session."""

    def __init__(self, url: str | None = None):
        self.url = url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._client: redis.Redis | None = None
        self._memory: dict[str, Any] = {}

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self.url, decode_responses=True)
        return self._client

    def _key(self, session_id: str, suffix: str) -> str:
        return f"coach:{session_id}:{suffix}"

    def save_state(self, session_id: str, state: dict[str, Any]) -> None:
        try:
            self.client.set(self._key(session_id, "state"), json.dumps(state), ex=7200)
        except redis.RedisError:
            self._memory["state"] = state

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        try:
            raw = self.client.get(self._key(session_id, "state"))
            return json.loads(raw) if raw else None
        except (redis.RedisError, json.JSONDecodeError):
            return self._memory.get("state")

    def mark_tip_sent(self, session_id: str, skill: str) -> None:
        key = self._key(session_id, f"tip:{skill}")
        try:
            self.client.set(key, "1", ex=120)
        except redis.RedisError:
            self._memory[f"tip:{skill}"] = True

    def was_tip_recently_sent(self, session_id: str, skill: str) -> bool:
        key = self._key(session_id, f"tip:{skill}")
        try:
            return bool(self.client.exists(key))
        except redis.RedisError:
            return bool(self._memory.get(f"tip:{skill}"))

    # ── 反馈闭环追踪 ──

    def record_advice_given(
        self, session_id: str, skill: str, event_name: str,
        advice_type: str = "", context: dict | None = None,
    ) -> None:
        """记录刚发出的建议，供后续检查是否被采纳."""
        payload = {
            "skill": skill,
            "event": event_name,
            "advice_type": advice_type,
            "context": context or {},
        }
        key = self._key(session_id, "last_advice")
        try:
            self.client.set(key, json.dumps(payload), ex=120)
        except redis.RedisError:
            self._memory["last_advice"] = payload

    def check_advice_followed(
        self, session_id: str, current_state: dict | None,
    ) -> tuple[bool, str]:
        """检查上次建议是否被采纳。返回 (followed, reason).

        按建议类型检测：
        - low_health → HP 恢复了 30%+ → 已回城/回复
        - survival/death → HP > 80% → 已复活/回血
        - item_purchased → 新增了对应分类的装备
        - dragon_soon → 玩家移动到龙坑附近
        """
        key = self._key(session_id, "last_advice")
        try:
            raw = self.client.get(key)
            if not raw:
                return False, "no_advice"
            advice = json.loads(raw)
            self.client.delete(key)  # 只检查一次
        except redis.RedisError:
            advice = self._memory.pop("last_advice", None)
            if not advice:
                return False, "no_advice"

        if not current_state:
            return False, "no_state"

        event = advice.get("event", "")
        context = advice.get("context", {})
        active = current_state.get("active_player", {})

        # low_health / death: 检查 HP 是否恢复
        if event in ("low_health", "death"):
            prev_hp = context.get("health_pct", 0)
            hp = active.get("health", 1)
            mx = active.get("max_health", 1)
            cur_hp_pct = hp / mx * 100 if mx > 0 else 100
            if cur_hp_pct > prev_hp + 30:
                return True, f"hp_recovered_{prev_hp:.0f}to{cur_hp_pct:.0f}"

        # item: 检查装备数量是否增加
        if event in ("item_purchased", "item_upgraded"):
            items = [it for it in active.get("items", []) if it.get("itemID", 0) != 0]
            prev_count = context.get("item_count", 0)
            if len(items) > prev_count:
                return True, f"item_added_{prev_count}to{len(items)}"

        return False, "not_detected"

    def adjust_skill_confidence(self, session_id: str, skill: str, followed: bool) -> float:
        """调整某 Skill 的置信度权重（0.5~1.5）."""
        key = self._key(session_id, f"conf:{skill}")
        current = 1.0
        try:
            raw = self.client.get(key)
            if raw:
                current = float(raw)
        except (redis.RedisError, ValueError):
            pass

        # 被采纳 +0.05, 未被采纳 -0.03
        delta = 0.05 if followed else -0.03
        new_val = max(0.5, min(1.5, current + delta))
        try:
            self.client.set(key, str(new_val), ex=86400)
        except redis.RedisError:
            pass
        return new_val
