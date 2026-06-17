"""事件预处理层 — 清洗无效事件 + 检测关键信号."""

import logging
import time

from memory.models import UserContext
from models.state import CoachEvent, GameState

logger = logging.getLogger(__name__)


class EventProcessor:
    def __init__(self, user_context: UserContext):
        self.context = user_context
        self._consecutive_deaths = 0
        self._last_death_time = 0.0

    # ── 公开入口 ──────────────────────────────

    def process(self, event: CoachEvent, state: GameState | None) -> dict | None:
        name = event.name

        # 清洗规则
        if not self._should_process(name, state):
            return None

        # 信号检测
        signals = self._detect_signals(name, event, state)

        # 更新内部状态
        self._update_internal(name, state)

        priority = self._calc_priority(name, signals)
        return {
            "event": event,
            "signals": signals,
            "priority": priority,
            "timestamp": time.time(),
        }

    def update_context(self, state: GameState):
        """从 GameState 同步用户上下文."""
        if not state:
            return
        ap = state.active_player
        self.context.current_champion = ap.summoner_name
        self.context.current_gold = ap.current_gold
        self.context.current_level = ap.level
        self.context.game_phase = self._guess_phase(state.game_time)

        # 从敌我玩家列表推断 role（简化：按位置判断）
        team = ""
        for p in state.all_players:
            if p.summoner_name == ap.summoner_name:
                team = p.team
                break
        if team:
            own_team = [p for p in state.all_players if p.team == team]
            role = self._infer_role(ap.summoner_name, own_team)
            if role:
                self.context.champion_role = role

        self.context.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ── 清洗规则 ──────────────────────────────

    def _should_process(self, name: str, state: GameState | None) -> bool:
        # 死亡时跳过非关键事件
        if state and state.active_player_health_pct() == 0:
            if name not in ("dragon_soon", "baron_soon"):
                return False
        return True

    # ── 信号检测 ──────────────────────────────

    def _detect_signals(self, name: str, event: CoachEvent, state: GameState | None) -> list[str]:
        signals: list[str] = []

        if name == "low_health":
            if self._consecutive_deaths >= 1:
                signals.append("repeatedly_low")
            health_pct = event.data.get("health_pct", 0)
            if health_pct < 15:
                signals.append("critically_low")

        if name == "dragon_soon" or name == "baron_soon":
            signals.append("objective_stage")
            seconds_left = event.data.get("seconds_left", 0)
            if seconds_left <= 10:
                signals.append("imminent_objective")

        if name == "item_purchased":
            signals.append("power_spike")

        # 经济信号（从 state 推测，简化版：只检测 active player 相对于平均值的差距）
        if state and len(state.all_players) >= 2:
            own_gold = state.active_player.current_gold
            enemy_golds = []
            for p in state.all_players:
                if p.summoner_name == state.active_player.summoner_name:
                    continue
                # 近似：同队伍的可能在 all_players 中，精确判断需要 team 字段
                enemy_golds.append(0)  # MVP 简化，不精确判断
            # 用当前总经济作为简单信号
            if own_gold > 3000:
                signals.append("strong_economy")

        return signals

    # ── 优先级计算 ─────────────────────────────

    def _calc_priority(self, name: str, signals: list[str]) -> int:
        base = {
            "low_health": 3,
            "dragon_soon": 2,
            "baron_soon": 2,
            "item_purchased": 1,
            "jungle_check": 1,
            "strategy_check": 1,
        }.get(name, 1)

        # 紧急信号提权
        if "imminent_objective" in signals:
            base = min(base + 1, 3)
        if "critically_low" in signals:
            base = 3
        if "repeatedly_low" in signals:
            base = max(base - 1, 1)  # 连死降权，别烦玩家

        return base

    # ── 内部状态 ──────────────────────────────

    def _update_internal(self, name: str, state: GameState | None):
        if name == "low_health":
            self._consecutive_deaths += 1
        else:
            self._consecutive_deaths = 0

    # ── 辅助 ──────────────────────────────────

    def _guess_phase(self, game_time: float) -> str:
        if game_time < 14 * 60:
            return "early"
        if game_time < 25 * 60:
            return "mid"
        return "late"

    def _infer_role(self, summoner_name: str, own_team: list) -> str:
        # MVP 简化：用 position 近似判断
        for p in own_team:
            if p.summoner_name == summoner_name:
                x, y = p.position.x, p.position.y
                # 召唤师峡谷大致坐标系
                if x > 8000:
                    return "bot"
                if x < 3000:
                    return "top"
                if 3000 <= x <= 8000 and y < 6000:
                    return "mid"
                if 3000 <= x <= 8000 and y >= 6000:
                    return "jungle"
        return ""
