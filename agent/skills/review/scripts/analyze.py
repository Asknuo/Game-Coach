"""
对局数据分析脚本 — 从 GameState 提取关键 KPI。

用法：
    from skills.review.scripts.analyze import analyze_game
    result = analyze_game(game_state_dict)
    print(result)
"""

from typing import Any


def analyze_game(game_state: dict[str, Any]) -> dict[str, Any]:
    """分析对局数据，返回结构化的复盘指标."""
    game_time = game_state.get("game_time", 0)
    minutes = max(game_time / 60, 1)

    active = game_state.get("active_player", {})

    # 基础指标
    level = active.get("level", 0)
    gold = active.get("current_gold", 0)
    hp = active.get("current_health", 0)
    max_hp = active.get("max_health", 1)
    hp_pct = hp / max_hp * 100 if max_hp else 0

    # 位置推断
    position = _infer_position(game_state)

    # CS 达标判断
    cs_target = _cs_target(position, minutes)
    cs_actual = _estimate_cs(level, gold, minutes)

    # 输出
    return {
        "game_time": game_time,
        "minutes": round(minutes, 1),
        "level": level,
        "gold": gold,
        "estimated_cs": cs_actual,
        "cs_target": cs_target,
        "cs_grade": "good" if cs_actual >= cs_target else "needs_work",
        "position": position,
        "hp_pct": round(hp_pct, 1),
    }


def _cs_target(position: str, minutes: float) -> float:
    """各位置 CS 目标（每分钟）."""
    targets = {"top": 8, "mid": 8, "adc": 8.5, "jungle": 6, "support": 0}
    return targets.get(position, 7) * minutes


def _estimate_cs(level: int, gold: int, minutes: float) -> float:
    """粗略估计 CS 数（基于等级和金币）."""
    # 无实际 CS 数据时的估算
    # 每个兵约 20g，等级大致对应 time*0.5
    return min(level * 12, minutes * 12)


def _infer_position(state: dict[str, Any]) -> str:
    """推断位置."""
    active = state.get("active_player", {})
    pos = active.get("position", {})
    if not pos:
        return "unknown"

    x = pos.get("x", 0) if isinstance(pos, dict) else 0
    y = pos.get("y", 0) if isinstance(pos, dict) else 0

    # 大致坐标 → 位置映射
    if y > 7000:
        return "top"
    elif y < 3000:
        return "adc" if x > 7000 else "support"
    else:
        return "mid" if 4000 < x < 7000 else "jungle"
