"""验证与发布节点 — validate（去重/置信度） / publish（时效性检查）."""

import logging

from graph.nodes.common import hp_pct
from graph.state import CoachState
from memory.redis_store import DEFAULT_TIP_TTL, MIN_CONFIDENCE_TO_PUBLISH

logger = logging.getLogger(__name__)


def _tip_stale_reason(
    event_name: str, event_data: dict, gs: dict, has_latest: bool,
) -> str:
    """时效性检查：返回应跳过发布的原因，空串表示仍然有效."""
    active = gs.get("active_player", {}) if gs else {}
    hp = hp_pct(active)

    # low_health: 血量已经恢复 > 50% → 取消
    if event_name == "low_health" and hp > 50:
        return "hp_recovered"

    # dragon_soon / baron_soon: 目标已出生（计时器消失）→ 提示已过时
    if event_name == "dragon_soon" and has_latest and not gs.get("dragon_timer"):
        return "objective_gone"
    if event_name == "baron_soon" and has_latest and not gs.get("baron_timer"):
        return "objective_gone"

    # item_purchased: 装备已不在栏位 → 取消
    # （仅检查己方购买；enemy_item_purchased 是敌方装备，与我方栏位无关）
    if event_name == "item_purchased" and active.get("items"):
        bought_id = event_data.get("item_id", 0)
        current_ids = {
            (it.get("item_id") or it.get("itemID", 0))
            for it in active.get("items", [])
        }
        if bought_id and bought_id not in current_ids:
            return "items_gone"

    return ""


class ValidationMixin:
    """去重验证与发布节点（依赖 redis_store，缺失时直接放行）."""

    async def validate(self, state: CoachState) -> CoachState:
        """去重 + 置信度检查，决定是否发布.

        - 去重窗口 = SKILL.md frontmatter 声明的 cooldown（不再固定 120s）
        - 置信度来自反馈闭环：玩家长期不采纳该 skill 的建议 → 自动降权停发
        """
        redis = self.deps.redis_store
        skill = state["skill_name"]

        if not skill:
            return {**state, "should_publish": False, "skip_reason": "no_skill"}

        if redis:
            # 反馈闭环消费：置信度过低的 skill 自动静音
            conf = await redis.get_skill_confidence(state["session_id"], skill)
            if conf < MIN_CONFIDENCE_TO_PUBLISH:
                logger.info("validate: skill=%s muted by low confidence %.2f", skill, conf)
                return {**state, "should_publish": False, "skip_reason": "low_confidence"}

            if await redis.was_tip_recently_sent(state["session_id"], skill):
                logger.debug("validate: duplicate skill=%s", skill)
                return {**state, "should_publish": False, "skip_reason": "duplicate"}

        return {**state, "should_publish": True, "skip_reason": ""}

    async def publish(self, state: CoachState) -> CoachState:
        """标记 tip 输出，含时效性检查（对比最新 state，而非事件时的旧快照）."""
        event_name = state.get("event_name", "")
        event_data = state.get("event_data", {})

        redis = self.deps.redis_store

        # 时效性检查：拉取最新 state — LLM 调用数秒后，事件时的快照可能已过时
        latest = None
        if redis:
            try:
                latest = await redis.get_state(state["session_id"])
            except Exception:
                latest = None
        gs = latest or state.get("game_state", {})

        if gs:
            reason = _tip_stale_reason(event_name, event_data, gs, latest is not None)
            if reason:
                logger.debug("publish: skip %s (%s)", event_name, reason)
                return {**state, "should_publish": False, "skip_reason": reason}

        if redis:
            # 去重 TTL = SKILL.md 声明的 cooldown
            from planner.planner import SKILL_REGISTRY
            meta = SKILL_REGISTRY.get(state["skill_name"], {})
            ttl = int(meta.get("cooldown", DEFAULT_TIP_TTL) or DEFAULT_TIP_TTL)
            await redis.mark_tip_sent(state["session_id"], state["skill_name"], ttl=ttl)

        state["tip"] = {
            "skill": state["skill_name"],
            "message": state["polished_message"],
            "priority": state["priority"],
        }
        logger.info("[%s] %s", state["skill_name"], state["polished_message"][:80])
        return state
