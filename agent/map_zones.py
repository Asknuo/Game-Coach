"""
位置区域语义化（Map Zone Resolver）

将 Live Client API 返回的 (X, Y) 坐标翻译为人类可读的区域名称。

LOL 召唤师峡谷地图坐标系：
  - 边界: X ∈ [0, ~14800], Y ∈ [0, ~14800]
  - 蓝队（ORDER）出生点: 左下角 (~0, ~0)
  - 红队（CHAOS）出生点: 右上角 (~14800, ~14800)
  - 河道中线: 大致 y = 6000~8000 带
  - 大龙坑: 约 (5000, 10400)
  - 小龙坑: 约 (9866, 4414)
  - 中路一塔蓝队: 约 (5048, 4812)
  - 中路一塔红队: 约 (9762, 8950)

用法:
    from map_zones import resolve_zone
    zone = resolve_zone(9866, 4414)  # → "小龙坑"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ── 地标坐标 ──

@dataclass(frozen=True)
class Landmark:
    name: str
    x: float
    y: float
    radius: float = 800  # 判定半径


LANDMARKS: list[Landmark] = [
    Landmark("泉水-蓝队", 0, 0, 2000),
    Landmark("泉水-红队", 14500, 14500, 2000),
    Landmark("基地-蓝队", 2000, 2000, 3000),
    Landmark("基地-红队", 12800, 12800, 3000),
    Landmark("小龙坑", 9866, 4414, 1000),
    Landmark("大龙坑", 5007, 10429, 1000),
    Landmark("上路河道入口", 6400, 10200, 800),
    Landmark("下路河道入口", 10200, 4600, 800),
    Landmark("蓝队红Buff", 7800, 4100, 800),
    Landmark("蓝队蓝Buff", 3800, 7800, 800),
    Landmark("红队红Buff", 7000, 10700, 800),
    Landmark("红队蓝Buff", 11000, 7000, 800),
    Landmark("峡谷先锋坑", 5007, 10429, 1000),  # 前期与大龙坑同位置
    Landmark("中路河道草丛", 7600, 7400, 600),
    Landmark("上路河道草丛", 5500, 10200, 600),
    Landmark("下路河道草丛", 10200, 4600, 600),
]


# ── 区域判定 ──

def resolve_zone(x: float, y: float, team: str = "") -> str:
    """将坐标 (x, y) 翻译为区域名称.

    Args:
        x: X 坐标
        y: Y 坐标
        team: 玩家队伍 ("ORDER"=蓝队 或 "CHAOS"=红队)，用于区分上下路

    Returns:
        区域名称，如 "蓝队野区"、"小龙坑"、"上路河道"
    """
    # 1. 优先检查是否在关键地标附近
    for lm in LANDMARKS:
        dist = ((x - lm.x) ** 2 + (y - lm.y) ** 2) ** 0.5
        if dist < lm.radius:
            return lm.name

    # 2. 按区域判定
    if x < 2000 and y < 2000:
        return "泉水-蓝队"
    if x > 12800 and y > 12800:
        return "泉水-红队"

    # 河道判定：y 在 5800~8800 带
    in_river = 5800 < y < 8800

    # 上下路判定
    is_top = y > 9000
    is_bot = y < 5800

    # 己方/对方半区
    if team == "CHAOS":  # 红队 = 右上
        my_jungle = x > 7500 and y > 7500
        enemy_jungle = x < 7500 or y < 7500
    else:  # ORDER = 蓝队 = 左下
        my_jungle = x < 7500 and y < 7500
        enemy_jungle = x > 7500 or y > 7500

    # 组合判定
    if in_river:
        if x < 4000:
            return "河道左侧"
        if x > 11000:
            return "河道右侧"
        if y > 7000:
            return "上路河道" if team == "CHAOS" else "大龙河道"
        return "下路河道" if team == "ORDER" else "小龙河道"

    if is_top:
        if my_jungle:
            return "己方上野区"
        if enemy_jungle:
            return "敌方上野区"
        return "上路" if team == "ORDER" else "下路"  # 红队视角的上路 = 地图下方

    if is_bot:
        if my_jungle:
            return "己方上野区" if team == "CHAOS" else "己方下野区"
        if enemy_jungle:
            return "敌方上野区" if team == "CHAOS" else "敌方下野区"
        return "下路" if team == "ORDER" else "上路"

    # 中路区域
    if 4000 < x < 11000:
        if y < 5000:
            return "中路偏下" if team == "ORDER" else "中路偏上"
        if y > 10000:
            return "中路偏上" if team == "ORDER" else "中路偏下"
        return "中路"

    # 野区
    if my_jungle:
        return "己方野区"
    return "敌方野区"


def get_active_zone(game_state: dict[str, Any]) -> str:
    """从 GameState dict 中推断玩家当前所在区域."""
    active = game_state.get("active_player", {})
    pos = active.get("position", {})
    x = pos.get("x", 0)
    y = pos.get("y", 0)
    team = active.get("team", "")
    if not team:
        # fallback: 从 all_players 查找
        name = active.get("summoner_name", "")
        for p in game_state.get("all_players", []):
            if isinstance(p, dict) and p.get("summoner_name") == name:
                team = p.get("team", "")
                break
    return resolve_zone(x, y, team)


def get_enemy_zones(game_state: dict[str, Any]) -> dict[str, str]:
    """返回所有敌方玩家位置区域: {summoner_name: zone_name}."""
    active = game_state.get("active_player", {})
    active_team = active.get("team", "")
    zones: dict[str, str] = {}
    for p in game_state.get("all_players", []):
        if not isinstance(p, dict):
            continue
        if p.get("team") == active_team:
            continue
        pos = p.get("position", {})
        x = pos.get("x", 0)
        y = pos.get("y", 0)
        if x == 0 and y == 0:
            continue  # 坐标无效
        name = p.get("summoner_name", "")
        zones[name] = resolve_zone(x, y, active_team)
    return zones
