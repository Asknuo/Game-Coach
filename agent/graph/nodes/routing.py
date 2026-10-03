"""Skill 路由节点 — route_skill + RAG 查询构建."""

import logging

from graph.state import CoachState

logger = logging.getLogger(__name__)


def _rag_query_enemy_item(state: CoachState, resolver) -> list[str]:
    """enemy_item_purchased：翻译 itemID→名称并写回 event_data，返回查询片段."""
    event_data = state.get("event_data", {})
    enemy_champ = event_data.get("enemy_champion", "")
    enemy_name = event_data.get("enemy_name", "")
    item_ids = event_data.get("item_ids", [])

    # 翻译 itemID → 物品名称
    item_names = resolver.get_names(item_ids)
    item_descs = [resolver.describe_item(iid) for iid in item_ids]

    # 把名称写回 event_data，后续节点（inject_memory、llm_polish）可直接用
    state["event_data"]["item_names"] = item_names
    state["event_data"]["item_descs"] = item_descs

    parts: list[str] = []
    if enemy_champ:
        parts.append(f"enemy {enemy_champ} {enemy_name} purchased {' '.join(item_names)} counter build counter items")
    if item_names:
        parts.append(f"items {' '.join(item_names)} counter")
    return parts


def _build_rag_query(state: CoachState) -> str:
    """按事件类型拼接 RAG 检索查询串（skill message + 事件上下文）."""
    from knowledge.item_resolver import get_item_resolver

    event_name = state["event_name"]
    event_data = state.get("event_data", {})
    # skill_message（面向用户的中文建议，含动态数字/英雄名）不进检索查询：
    # 1) 动态文本让 embed LRU 每次都 miss；2) KB 是英文语料，
    #    英文事件片段才是检索信号，中文建议文本只会污染 embedding
    parts: list[str] = []

    if event_name == "dragon_soon":
        parts.append("dragon fight positioning objective strategy")
    elif event_name == "baron_soon":
        parts.append("baron fight positioning objective strategy")
    elif event_name == "low_health":
        parts.append("when low health recall sustain laning recovery")
    elif event_name == "item_purchased":
        parts.append("recommended next items build order")
    elif event_name == "item_sold":
        name = get_item_resolver().get_name(event_data.get("item_id", 0))
        parts.append(f"sold {name} freed inventory slot next item build order")
    elif event_name == "item_upgraded":
        resolver = get_item_resolver()
        old_name = resolver.get_name(event_data.get("old_item_id", 0))
        new_name = resolver.get_name(event_data.get("new_item_id", 0))
        parts.append(f"upgraded {old_name} to {new_name} completed item powerspike")
    elif event_name == "enemy_item_purchased":
        parts.extend(_rag_query_enemy_item(state, get_item_resolver()))
    elif event_name == "kill":
        # 查询串不拼动态数字（total_kills/gold_gap/kills）：数字会让每次的
        # 字符串都不同，LRU 缓存永远 miss；且数字对 embedding 语义几乎无贡献
        parts.append("after getting a kill what to do objective push tower dragon capitalize advantage")
    elif event_name == "enemy_gold_lead":
        enemy_champ = event_data.get("enemy_champion", "")
        parts.append(f"enemy {enemy_champ} has a gold lead how to play from behind counter fed enemy")
    elif event_name == "enemy_fed":
        enemy_champ = event_data.get("enemy_champion", "")
        parts.append(f"enemy {enemy_champ} is fed how to shut down fed enemy shutdown target counter")
    elif event_name == "gold_spike":
        parts.append("gold advantage power spike next item purchase timing")
    elif event_name == "laning_check":
        parts.append("laning wave management trade timing minion control")
    elif event_name == "macro_check":
        parts.append("macro rotations objective priority map movement")
    elif event_name == "teamfight_detected":
        parts.append("teamfight positioning target priority focus frontline backline")
    else:
        parts.append("strategy tips priority")

    return " ".join(parts)


class RoutingMixin:
    """事件 → Skill 路由节点（依赖 planner）."""

    def route_skill(self, state: CoachState) -> CoachState:
        """事件名 → Skill，加载 SKILL.md 上下文和坑点清单."""
        planner = self.deps.planner

        from models.state import CoachEvent, GameState
        from planner.planner import get_skill_context, get_skill_gotchas

        event = CoachEvent(
            name=state["event_name"],
            data=state["event_data"],
        )
        gs = state.get("game_state")
        try:
            snapshot = GameState.model_validate(gs) if gs else None
        except Exception:
            snapshot = None
        tip = planner.plan(event, snapshot)

        if not tip:
            logger.debug("route_skill: %s → no skill matched", state["event_name"])
            return {**state, "is_valid": False, "skip_reason": "no_skill"}

        skill_name = tip.skill

        # ── 加载 SKILL.md 正文和坑点清单 ──
        skill_context = get_skill_context(skill_name)
        skill_gotchas = get_skill_gotchas(skill_name)

        state["skill_name"] = skill_name
        state["skill_message"] = tip.message
        state["skill_context"] = skill_context
        state["skill_gotchas"] = skill_gotchas

        logger.debug(
            "route_skill: %s → %s (context: %d chars, gotchas: %d chars)",
            state["event_name"], skill_name,
            len(skill_context), len(skill_gotchas),
        )

        state["rag_query"] = _build_rag_query(state)
        return state
