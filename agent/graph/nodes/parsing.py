"""解析与信号检测节点 — parse_event / detect_signals（无外部依赖）."""

import logging

from graph.nodes.common import OBJECTIVE_EVENTS, hp_pct, is_in_fountain
from graph.state import CoachState

logger = logging.getLogger(__name__)

# 静态信号表：事件名 → (优先级下限, 附加信号)
_SIGNAL_TABLE: dict[str, tuple[int, list[str]]] = {
    "dragon_soon": (2, ["objective_stage"]),
    "baron_soon": (2, ["objective_stage"]),
    "item_purchased": (1, ["power_spike"]),
    "item_upgraded": (1, ["power_spike"]),
    "gold_spike": (1, ["power_spike"]),
    "kill": (2, ["kill_secured"]),
    "death": (2, ["player_died"]),
    "teamfight_detected": (2, ["teamfight"]),
    "enemy_gold_lead": (2, ["enemy_power_spike", "danger"]),
    "enemy_fed": (3, ["enemy_fed", "danger"]),
    "enemy_item_purchased": (1, ["enemy_power_spike"]),
}


class ParsingMixin:
    """事件解析与信号清洗节点（纯函数，不读 deps）."""

    def parse_event(self, state: CoachState) -> CoachState:
        """解析原始事件，提取 event_name / event_data."""
        event = state.get("event", {})
        event_name = event.get("name", "")
        event_data = event.get("data", {})

        logger.debug("parse_event: %s", event_name)
        return {
            **state,
            "event_name": event_name,
            "event_data": event_data,
            "is_valid": bool(event_name),
            "signals": [],
            # priority 保留上游（队列/紧急通道）计算值，detect_signals 再按事件上调
            "skill_name": "",
            "skill_message": "",
            "rag_query": "",
            "rag_docs": [],
            "memory_context": "",
            "skill_context": "",     # SKILL.md 正文
            "skill_gotchas": "",     # 坑点清单
            "polished_message": "",
            "should_publish": False,
            "skip_reason": "",
            "tip": None,
        }

    def detect_signals(self, state: CoachState) -> CoachState:
        """清洗无效事件 + 检测关键信号 + 计算优先级."""
        name = state["event_name"]
        data = state["event_data"]
        gs = state.get("game_state", {})

        # 上游（event_priority / SKILL_REGISTRY）已给出基础优先级，这里只做上调
        priority = state.get("priority", 1) or 1

        active = gs.get("active_player", {}) if gs else {}
        hp = hp_pct(active)

        # 死亡时跳过非龙/大龙事件
        if hp == 0 and name not in OBJECTIVE_EVENTS:
            logger.debug("detect_signals: skip (dead) %s", name)
            return {**state, "is_valid": False, "skip_reason": "player_dead"}

        if name == "low_health":
            # 上下文抑制：在泉水附近说明已回城/回复中，不发
            if is_in_fountain(active):
                logger.debug("detect_signals: skip low_health (in fountain)")
                return {**state, "is_valid": False, "skip_reason": "in_fountain"}
            priority = max(priority, 3)
            signals = ["low_health"]
            if hp < 15:
                signals.append("critically_low")
        else:
            floor, base_signals = _SIGNAL_TABLE.get(name, (1, []))
            signals = list(base_signals)
            priority = max(priority, floor)
            # 目标临近：10s 内出生 → 提到最高优先级
            if name in OBJECTIVE_EVENTS and data.get("seconds_left", 99) <= 10:
                signals.append("imminent_objective")
                priority = 3

        logger.debug("detect_signals: %s signals=%s priority=%d", name, signals, priority)
        return {**state, "signals": signals, "priority": priority, "is_valid": True}
