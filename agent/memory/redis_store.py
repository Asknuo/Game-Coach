"""短期记忆 / 会话缓存 — Redis（异步客户端）。

职责:
- 最新 GameState 缓存（publish 时效性检查、tips/latest 端点）
- tip 去重标记（TTL = skill cooldown）
- 反馈闭环：记录建议 → 后续 state 帧检查是否被采纳 → 调整 skill 置信度

所有方法均为 async；Redis 不可用时降级为进程内 dict（仅兜底，不保证一致）。
"""

import json
import logging
import os
import time
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# 建议发出后，在这么多秒内持续观察玩家是否采纳
ADVICE_FEEDBACK_WINDOW = 25.0
# 置信度低于该值时 validate 节点将跳过发布（反馈闭环真正参与决策）
MIN_CONFIDENCE_TO_PUBLISH = 0.7

DEFAULT_TIP_TTL = 120  # skill 未声明 cooldown 时的去重窗口


class RedisStore:
    """Short-term memory for the current game session (async)."""

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

    # ── State 缓存 ──

    async def save_state(self, session_id: str, state: dict[str, Any]) -> None:
        try:
            await self.client.set(self._key(session_id, "state"), json.dumps(state), ex=7200)
        except redis.RedisError:
            self._memory["state"] = state

    async def get_state(self, session_id: str) -> dict[str, Any] | None:
        try:
            raw = await self.client.get(self._key(session_id, "state"))
            return json.loads(raw) if raw else None
        except (redis.RedisError, json.JSONDecodeError):
            return self._memory.get("state")

    # ── Tip 去重（TTL 由 skill cooldown 驱动） ──

    async def mark_tip_sent(self, session_id: str, skill: str, ttl: int = DEFAULT_TIP_TTL) -> None:
        key = self._key(session_id, f"tip:{skill}")
        try:
            await self.client.set(key, "1", ex=max(ttl, 1))
        except redis.RedisError:
            self._memory[f"tip:{skill}"] = time.time() + ttl

    async def was_tip_recently_sent(self, session_id: str, skill: str) -> bool:
        key = self._key(session_id, f"tip:{skill}")
        try:
            return bool(await self.client.exists(key))
        except redis.RedisError:
            expiry = self._memory.get(f"tip:{skill}")
            return bool(expiry and expiry > time.time())

    # ── 反馈闭环追踪 ──

    async def record_advice_given(
        self, session_id: str, skill: str, event_name: str,
        advice_type: str = "", context: dict | None = None,
    ) -> None:
        """记录刚发出的建议，供后续 state 帧检查是否被采纳."""
        payload = {
            "skill": skill,
            "event": event_name,
            "advice_type": advice_type,
            "context": context or {},
            "ts": time.time(),
        }
        key = self._key(session_id, "last_advice")
        try:
            await self.client.set(key, json.dumps(payload), ex=int(ADVICE_FEEDBACK_WINDOW) + 30)
        except redis.RedisError:
            self._memory["last_advice"] = payload

    async def check_advice_followed(
        self, session_id: str, current_state: dict | None,
    ) -> tuple[str, str, str]:
        """检查上次建议是否被采纳。

        返回 (status, skill, reason):
        - "followed"   → 玩家已采纳（建议记录被消费）
        - "pending"    → 仍在观察窗口内，下帧继续检查（记录保留）
        - "expired"    → 超过观察窗口未采纳（建议记录被消费）
        - "no_advice"  → 没有待检查的建议
        - "no_state"   → 当前无 state 可比对
        """
        key = self._key(session_id, "last_advice")
        advice = None
        via_redis = True
        try:
            raw = await self.client.get(key)
            if raw:
                advice = json.loads(raw)
        except (redis.RedisError, json.JSONDecodeError):
            via_redis = False
            advice = self._memory.get("last_advice")

        if not advice:
            return "no_advice", "", ""
        if not current_state:
            return "no_state", advice.get("skill", ""), ""

        skill = advice.get("skill", "")
        event = advice.get("event", "")
        context = advice.get("context", {})
        age = time.time() - advice.get("ts", 0)
        active = current_state.get("active_player", {})

        followed, reason = self._judge_followed(event, context, active)
        if followed:
            await self._consume_advice(key, via_redis)
            return "followed", skill, reason

        if age >= ADVICE_FEEDBACK_WINDOW:
            await self._consume_advice(key, via_redis)
            return "expired", skill, f"not_followed_in_{ADVICE_FEEDBACK_WINDOW:.0f}s"

        return "pending", skill, "observing"

    async def _consume_advice(self, key: str, via_redis: bool) -> None:
        if via_redis:
            try:
                await self.client.delete(key)
                return
            except redis.RedisError:
                pass
        self._memory.pop("last_advice", None)

    @staticmethod
    def _judge_followed(event: str, context: dict, active: dict) -> tuple[bool, str]:
        """按建议类型判断玩家是否采纳."""
        # low_health / death: HP 明显恢复 → 已回城/回复
        if event in ("low_health", "death"):
            prev_hp = context.get("health_pct", 0)
            hp = active.get("health", 1)
            mx = active.get("max_health", 1)
            cur_hp_pct = hp / mx * 100 if mx > 0 else 100
            if cur_hp_pct > prev_hp + 30:
                return True, f"hp_recovered_{prev_hp:.0f}to{cur_hp_pct:.0f}"

        # item: 装备数量增加
        if event in ("item_purchased", "item_upgraded"):
            items = [
                it for it in active.get("items", [])
                if (it.get("item_id") or it.get("itemID", 0)) != 0
            ]
            prev_count = context.get("item_count", 0)
            if len(items) > prev_count:
                return True, f"item_added_{prev_count}to{len(items)}"

        return False, "not_detected"

    # ── Skill 置信度（反馈闭环的输出，validate 节点消费） ──

    async def get_skill_confidence(self, session_id: str, skill: str) -> float:
        key = self._key(session_id, f"conf:{skill}")
        try:
            raw = await self.client.get(key)
            return float(raw) if raw else 1.0
        except (redis.RedisError, ValueError):
            return float(self._memory.get(f"conf:{skill}", 1.0))

    async def adjust_skill_confidence(self, session_id: str, skill: str, followed: bool) -> float:
        """调整某 Skill 的置信度权重（0.5~1.5）."""
        if not skill:
            return 1.0
        current = await self.get_skill_confidence(session_id, skill)
        # 被采纳 +0.05, 未被采纳 -0.03
        delta = 0.05 if followed else -0.03
        new_val = max(0.5, min(1.5, current + delta))
        key = self._key(session_id, f"conf:{skill}")
        try:
            await self.client.set(key, str(new_val), ex=86400)
        except redis.RedisError:
            self._memory[f"conf:{skill}"] = new_val
        return new_val
