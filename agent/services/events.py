"""事件分类与分发辅助 — 紧急度 / 优先级 / 死亡过滤 / LCU 大厅事件 / 记忆同步."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from game.zones import get_active_zone, get_enemy_zones
from graph.nodes.common import DEAD_PASSTHROUGH_EVENTS
from models.state import CoachEvent, GameState
from planner.planner import EVENT_TO_SKILL, SKILL_REGISTRY

if TYPE_CHECKING:
    from context import AppContext

logger = logging.getLogger(__name__)

# LCU 大厅事件（仅作上下文，不进 coaching 流水线）
_LCU_CONTEXT_EVENTS = frozenset({
    "lcu_connected",
    "gameflow_phase_change",
    "lcu_pick_phase",
    "lcu_runes_updated",
    "lcu_mastery_loaded",
})


def is_urgent(event: CoachEvent) -> bool:
    """判断是否紧急事件（绕过防抖队列，立即处理）."""
    name = event.name
    data = event.data
    if name in ("low_health", "death"):
        return True
    return name in ("dragon_soon", "baron_soon") and data.get("seconds_left", 99) <= 30


def game_phase(game_time: float) -> str:
    if game_time < 14 * 60:
        return "early"
    if game_time < 25 * 60:
        return "mid"
    return "late"


def event_priority(event: CoachEvent) -> int:
    skill_name = EVENT_TO_SKILL.get(event.name, "")
    meta = SKILL_REGISTRY.get(skill_name, {})
    prio = meta.get("priority", 1)
    if event.name in ("dragon_soon", "baron_soon") and event.data.get("seconds_left", 99) <= 10:
        return 3
    return prio


def should_skip_dead_event(state: GameState | None, event: CoachEvent) -> bool:
    if not state:
        return False
    # 目标类与 death 自身穿透：龙/大龙与生死无关；death 是 survival
    # 复活期建议的触发源（曾被此处吞掉，见 BUGFIX.md）
    if event.name in DEAD_PASSTHROUGH_EVENTS:
        return False
    return state.active_player_health_pct() == 0


async def handle_lcu_event(ctx: AppContext, event: CoachEvent) -> bool:
    """处理 LCU 大厅事件：符文/熟练度/选人等写入记忆上下文，供注入 LLM.

    返回 True 表示该事件已被消费（不再进入 coaching 流水线）。
    """
    memory = ctx.memory
    name = event.name
    if name == "lcu_game_start":
        # 新一局开始：清掉上一局的会话级状态——Redis 的 tip 冷却与
        # conf:*（TTL 24h）会让新局前几分钟的建议被静音；top_of_mind 与
        # 对局期上下文也是上一局的残留
        await _reset_session_state(ctx)
        data = event.data
        runes = data.get("runes") or {}
        if runes:
            memory.user.context["runes"] = runes
        masteries = data.get("top_masteries") or []
        if masteries:
            memory.user.context["top_masteries"] = masteries[:5]
        if data.get("summoner_name"):
            memory.user.context["summoner_name"] = data["summoner_name"]
        logger.info("LCU game start context: runes=%d, masteries=%d (session reset)",
                    len(runes.get("perk_ids", [])), len(masteries))
        return True

    if name == "lcu_champion_picked":
        memory.user.context["assigned_position"] = event.data.get("assigned_position", "")
        memory.user.context["champion_id"] = event.data.get("champion_id", 0)
        return True

    if name in _LCU_CONTEXT_EVENTS:
        logger.debug("LCU event %s (context only)", name)
        return True

    return False


async def _reset_session_state(ctx: AppContext) -> None:
    """重置会话级状态：Redis 去重/冷却/置信度/state + 进程内对局期记忆."""
    redis_store = getattr(ctx, "redis_store", None)
    if redis_store is not None:
        try:
            await redis_store.reset_session("default")
        except Exception:
            logger.exception("session reset failed (redis)")

    memory = ctx.memory
    memory.user.top_of_mind.clear()
    for key in ("current_zone", "enemy_zones", "runes", "top_masteries",
                "assigned_position", "champion_id", "summoner_name"):
        memory.user.context.pop(key, None)

    metrics = getattr(ctx, "metrics", None)
    if metrics is not None:
        metrics["session_resets"] = metrics.get("session_resets", 0) + 1


def update_memory_from_state(ctx: AppContext, state: GameState, payload: dict) -> None:
    """每帧 state 同步玩家画像到记忆（英雄/金币/等级/阶段/KDA/地图区域）."""
    memory = ctx.memory
    ap = state.active_player
    # champion 字段存英雄名（champion_name 已由 sync_active_player 补全），
    # 召唤师名另存 context，两者不再混用
    memory.user.current_champion = ap.champion_name or ap.summoner_name
    memory.user.context["summoner_name"] = ap.summoner_name
    memory.user.current_gold = ap.current_gold
    memory.user.current_level = ap.level
    memory.user.game_phase = game_phase(state.game_time)
    # KDA 同步（复盘与记忆注入都依赖）
    memory.user.kda = {"kills": ap.kills, "deaths": ap.deaths, "assists": ap.assists}
    memory.user.context["current_zone"] = get_active_zone(payload)
    memory.user.context["enemy_zones"] = get_enemy_zones(payload)
