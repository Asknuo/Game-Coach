"""节点共享的纯函数工具."""

# 死亡时仍需推送的目标类事件
OBJECTIVE_EVENTS = ("dragon_soon", "baron_soon")


def hp_pct(active: dict) -> float:
    """活跃玩家的血量百分比（max_hp<=0 时视为满血）."""
    hp = active.get("health", 1)
    max_hp = active.get("max_health", 1)
    return hp / max_hp * 100 if max_hp > 0 else 100


def is_in_fountain(active: dict) -> bool:
    """玩家是否在自家泉水附近（已回城/回复中，低血量提示无意义）.

    蓝队泉水 ≈ (0,0)，红队泉水 ≈ (14500,14500)，队伍未知时两侧都查.
    """
    from models.state import get_position_distance, is_position_valid

    active_pos = active.get("position", {"x": 0, "y": 0})
    if not is_position_valid(active_pos):
        return False

    team = active.get("team", "")
    if team == "CHAOS":
        fountains = [{"x": 14500, "y": 14500}]
    elif team == "ORDER":
        fountains = [{"x": 0, "y": 0}]
    else:
        fountains = [{"x": 0, "y": 0}, {"x": 14500, "y": 14500}]

    return any(get_position_distance(active_pos, f) < 1500 for f in fountains)
