"""
事件检测器（Event Detector）
- 从 Go Collector 发来的 GameState 中检测高级事件
- 提取自 agent/collector/live_client.py 的核心检测逻辑
- 由 Agent 的 WebSocket handler 在每次收到 state 时调用
- 与 Go Collector 互补：Go 负责基础事件（low_health/dragon_soon等），
  Python Detector 负责高级事件（death/kill/enemy_item/item_sold/item_upgraded等）

用法:
    detector = EventDetector()
    events = detector.detect(state_dict)   # → [{"name": "kill", "data": {...}}, ...]
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class EventDetector:
    """接收 Go Collector 发来的 GameState dict，对比快照检测事件.

    与 Go Collector 的分工：
    Go Collector 已经处理: low_health, item_purchased, dragon_soon, baron_soon,
                          jungle_check, strategy_check
    本 Detector 补充:    death, kill, enemy_item_purchased, item_sold,
                          item_upgraded, gold_spike
    """

    def __init__(self):
        # 我方快照
        self._last_hp: float = 100.0
        self._last_deaths: int = 0
        self._last_items_snapshot: dict = {}  # {"by_slot": {slot: itemID}, "ids": [itemID]}
        self._last_gold: float = 0
        self._kills_snapshot: dict[str, int] = {}  # summoner_name → kills

        # 敌方装备快照: {enemy_name: [itemID, ...]}
        self._enemy_items_snapshot: dict[str, list[int]] = {}

    def detect(self, game_state: dict[str, Any]) -> list[dict[str, Any]]:
        """对比快照，返回检测到的事件列表.

        Args:
            game_state: Go Collector 发来的完整 GameState dict
        Returns:
            [{name: "kill", data: {...}}, ...]
        """
        events: list[dict[str, Any]] = []

        active = game_state.get("active_player", {})
        if not active:
            return events

        # 1. 低血量 — Go Collector 已在处理，这里做副本抑制
        hp_pct = self._calc_hp_pct(active)
        if hp_pct < 30 and self._last_hp >= 30:
            events.append({
                "name": "low_health",
                "data": {"health_pct": round(hp_pct, 1), "source": "python_detector"},
            })
        self._last_hp = hp_pct

        # 2. 死亡 — Go Collector 未检测
        deaths = active.get("deaths", 0) if isinstance(active, dict) else getattr(active, "deaths", 0)
        if deaths > self._last_deaths:
            diff = deaths - self._last_deaths
            for _ in range(diff):
                events.append({
                    "name": "death",
                    "data": {
                        "total_deaths": deaths,
                        "game_time": game_state.get("game_time", 0),
                    },
                })
        self._last_deaths = deaths

        # 3. 装备变化（purchased / sold / upgraded）
        events.extend(self._detect_my_items(active))

        # 4. 金币变化
        gold = active.get("current_gold", 0) if isinstance(active, dict) else getattr(active, "current_gold", 0)
        if self._last_gold > 0 and gold > 0:
            gold_delta = gold - self._last_gold
            if gold_delta > 500:
                events.append({
                    "name": "gold_spike",
                    "data": {"current_gold": gold, "delta": round(gold_delta)},
                })
        self._last_gold = gold

        # 5. 击杀
        events.extend(self._detect_kills(game_state, active))

        # 6. 敌方装备变化
        events.extend(self._detect_enemy_items(game_state))

        return events

    # ── 内部方法 ──

    @staticmethod
    def _calc_hp_pct(player: dict) -> float:
        hp = player.get("health", 1)
        mx = player.get("max_health", 1)
        if mx <= 0:
            return 100
        return hp / mx * 100

    def _detect_my_items(self, active: dict) -> list[dict]:
        events: list[dict] = []
        current_by_slot: dict[int, int] = {}
        current_ids: list[int] = []

        for it in active.get("items", []):
            if not isinstance(it, dict):
                continue
            iid = it.get("itemID", 0)
            slot = it.get("slot", 0)
            if iid != 0:
                current_by_slot[slot] = iid
                current_ids.append(iid)

        prev_by_slot = self._last_items_snapshot.get("by_slot", {})
        prev_ids = set(self._last_items_snapshot.get("ids", []))

        new_ids = [iid for iid in current_ids if iid not in prev_ids]
        removed_ids = [iid for iid in prev_ids if iid not in current_ids]
        upgraded: dict[int, tuple[int, int]] = {}

        for slot, new_id in current_by_slot.items():
            old_id = prev_by_slot.get(slot, 0)
            if old_id != 0 and old_id != new_id and new_id != 0:
                upgraded[slot] = (old_id, new_id)

        # 纯新增
        upgraded_news = {t[1] for t in upgraded.values()}
        for iid in new_ids:
            if iid not in upgraded_news:
                events.append({
                    "name": "item_purchased",
                    "data": {"item_id": iid, "action": "purchased"},
                })

        # 纯卖出
        upgraded_olds = {t[0] for t in upgraded.values()}
        for iid in removed_ids:
            if iid not in upgraded_olds:
                events.append({
                    "name": "item_sold",
                    "data": {"item_id": iid, "action": "sold_or_consumed"},
                })

        # 合成
        for slot, (old_id, new_id) in upgraded.items():
            events.append({
                "name": "item_upgraded",
                "data": {"slot": slot, "old_item_id": old_id, "new_item_id": new_id, "action": "upgraded"},
            })

        self._last_items_snapshot = {"by_slot": current_by_slot, "ids": current_ids}
        return events

    def _detect_kills(self, game_state: dict, active: dict) -> list[dict]:
        events: list[dict] = []
        ap_name = active.get("summoner_name", "")

        current_kills: dict[str, int] = {}
        for p in game_state.get("all_players", []):
            if not isinstance(p, dict):
                continue
            name = p.get("summoner_name", "")
            kills = p.get("kills", 0)
            current_kills[name] = kills

        for name, kills in current_kills.items():
            prev = self._kills_snapshot.get(name, 0)
            if kills > prev and name == ap_name:
                events.append({
                    "name": "kill",
                    "data": {
                        "killer": name,
                        "total_kills": kills,
                        "game_time": game_state.get("game_time", 0),
                    },
                })

        self._kills_snapshot = current_kills
        return events

    def _detect_enemy_items(self, game_state: dict) -> list[dict]:
        events: list[dict] = []
        active = game_state.get("active_player", {})
        if not active:
            return events

        active_team = self._get_player_team(active, game_state)

        for p in game_state.get("all_players", []):
            if not isinstance(p, dict):
                continue
            p_team = p.get("team", "")
            if p_team == active_team:
                continue

            enemy_name = p.get("summoner_name", "")
            if not enemy_name:
                continue

            current_items = [
                it.get("itemID", 0)
                for it in p.get("items", [])
                if isinstance(it, dict) and it.get("itemID", 0) != 0
            ]
            prev_items = self._enemy_items_snapshot.get(enemy_name, [])

            added = [iid for iid in current_items if iid not in prev_items]
            removed = [iid for iid in prev_items if iid not in current_items]

            if added:
                events.append({
                    "name": "enemy_item_purchased",
                    "data": {
                        "enemy_name": enemy_name,
                        "enemy_champion": p.get("champion_name", ""),
                        "item_ids": added,
                    },
                })
            if removed:
                events.append({
                    "name": "enemy_item_sold",
                    "data": {
                        "enemy_name": enemy_name,
                        "item_ids": removed,
                    },
                })

            self._enemy_items_snapshot[enemy_name] = current_items

        return events

    @staticmethod
    def _get_player_team(player: dict, game_state: dict) -> str:
        """从 GameState 推断我方队伍."""
        team = player.get("team", "")
        if team:
            return team
        name = player.get("summoner_name", "")
        for p in game_state.get("all_players", []):
            if isinstance(p, dict) and p.get("summoner_name") == name:
                return p.get("team", "")
        return ""


# ── 模块级单例 ──

_detector: EventDetector | None = None


def get_detector() -> EventDetector:
    global _detector
    if _detector is None:
        _detector = EventDetector()
    return _detector
