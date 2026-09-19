"""graph/nodes/ 关键节点逻辑测试."""

import pytest

from graph import GraphDeps, GraphNodes

# ── 测试辅助 ──────────────────────────────────────────

class FakeRedisStore:
    """内存版 RedisStore，覆盖流水线用到的全部方法."""

    def __init__(self, state=None, confidence=1.0, recently_sent=False):
        self._state = state
        self._confidence = confidence
        self._recently_sent = recently_sent
        self.marked: list[tuple[str, int]] = []

    async def get_state(self, session_id):
        return self._state

    async def get_skill_confidence(self, session_id, skill):
        return self._confidence

    async def was_tip_recently_sent(self, session_id, skill):
        return self._recently_sent

    async def mark_tip_sent(self, session_id, skill, ttl=120):
        self.marked.append((skill, ttl))


def _nodes(redis_store=None) -> GraphNodes:
    """构建仅注入 FakeRedisStore 的节点集合（其余依赖缺省即降级）."""
    return GraphNodes(GraphDeps(redis_store=redis_store))


def _game_state(hp=100.0, max_hp=1000.0, x=5000, y=5000, team="CHAOS",
                items=None, dragon_timer=None):
    return {
        "game_time": 300.0,
        "active_player": {
            "summoner_name": "me",
            "champion_name": "Ahri",
            "team": team,
            "health": hp,
            "max_health": max_hp,
            "position": {"x": x, "y": y},
            "items": items or [],
        },
        "all_players": [],
        "dragon_timer": dragon_timer,
    }


def _coach_state(event_name, event_data=None, game_state=None, priority=1, **over):
    state = {
        "event": {"name": event_name, "data": event_data or {}},
        "game_state": game_state,
        "session_id": "test",
        "event_name": event_name,
        "event_data": event_data or {},
        "signals": [],
        "priority": priority,
        "is_valid": True,
        "skill_name": "survival",
        "skill_message": "draft",
        "rag_query": "",
        "rag_docs": [],
        "memory_context": "",
        "skill_context": "",
        "skill_gotchas": "",
        "polished_message": "polished",
        "should_publish": False,
        "skip_reason": "",
        "tip": None,
    }
    state.update(over)
    return state


# ── detect_signals ────────────────────────────────────

class TestDetectSignals:
    def test_red_team_fountain_suppresses_low_health(self):
        """红方玩家在自家泉水 (~14500,14500) 时 low_health 应被抑制."""
        gs = _game_state(hp=200, max_hp=1000, x=14400, y=14400, team="CHAOS")
        out = _nodes().detect_signals(_coach_state("low_health", game_state=gs))
        assert out["is_valid"] is False
        assert out["skip_reason"] == "in_fountain"

    def test_blue_team_fountain_suppresses_low_health(self):
        """蓝方玩家在自家泉水 (~0,0) 时 low_health 应被抑制."""
        gs = _game_state(hp=200, max_hp=1000, x=300, y=300, team="ORDER")
        out = _nodes().detect_signals(_coach_state("low_health", game_state=gs))
        assert out["is_valid"] is False
        assert out["skip_reason"] == "in_fountain"

    def test_low_health_in_lane_passes(self):
        gs = _game_state(hp=200, max_hp=1000, x=7000, y=7000, team="CHAOS")
        out = _nodes().detect_signals(_coach_state("low_health", game_state=gs))
        assert out["is_valid"] is True
        assert "low_health" in out["signals"]
        assert out["priority"] == 3

    def test_dead_player_skips_non_objective_events(self):
        gs = _game_state(hp=0, max_hp=1000)
        out = _nodes().detect_signals(_coach_state("kill", game_state=gs))
        assert out["is_valid"] is False
        assert out["skip_reason"] == "player_dead"

    def test_dead_player_still_gets_dragon_event(self):
        gs = _game_state(hp=0, max_hp=1000)
        out = _nodes().detect_signals(_coach_state(
            "dragon_soon", event_data={"seconds_left": 20}, game_state=gs))
        assert out["is_valid"] is True

    def test_priority_only_escalates(self):
        """上游给的高优先级不被节点降级."""
        gs = _game_state(hp=900, max_hp=1000)
        out = _nodes().detect_signals(_coach_state("laning_check", game_state=gs, priority=3))
        assert out["priority"] == 3


# ── validate ──────────────────────────────────────────

class TestValidate:
    @pytest.mark.asyncio
    async def test_low_confidence_mutes_skill(self):
        fake = FakeRedisStore(confidence=0.6)
        out = await _nodes(fake).validate(_coach_state("low_health"))
        assert out["should_publish"] is False
        assert out["skip_reason"] == "low_confidence"

    @pytest.mark.asyncio
    async def test_duplicate_skill_skipped(self):
        fake = FakeRedisStore(recently_sent=True)
        out = await _nodes(fake).validate(_coach_state("low_health"))
        assert out["should_publish"] is False
        assert out["skip_reason"] == "duplicate"

    @pytest.mark.asyncio
    async def test_normal_passes(self):
        fake = FakeRedisStore()
        out = await _nodes(fake).validate(_coach_state("low_health"))
        assert out["should_publish"] is True


# ── publish ───────────────────────────────────────────

class TestPublish:
    @pytest.mark.asyncio
    async def test_low_health_cancelled_when_hp_recovered(self):
        """发布前血量已恢复 → 建议应被取消（对比最新 state，非旧快照）."""
        old_snapshot = _game_state(hp=200, max_hp=1000)
        latest = _game_state(hp=900, max_hp=1000)  # 最新 state: 已回血
        fake = FakeRedisStore(state=latest)
        out = await _nodes(fake).publish(_coach_state("low_health", game_state=old_snapshot))
        assert out.get("tip") is None
        assert out["skip_reason"] == "hp_recovered"

    @pytest.mark.asyncio
    async def test_dragon_tip_cancelled_when_timer_gone(self):
        """龙已出生（timer 消失）→ dragon_soon 提示应被取消."""
        old_snapshot = _game_state(dragon_timer={"seconds_left": 20})
        latest = _game_state(dragon_timer=None)
        fake = FakeRedisStore(state=latest)
        out = await _nodes(fake).publish(_coach_state(
            "dragon_soon", event_data={"seconds_left": 20},
            game_state=old_snapshot, skill_name="dragon"))
        assert out.get("tip") is None
        assert out["skip_reason"] == "objective_gone"

    @pytest.mark.asyncio
    async def test_item_purchase_confirmed_with_item_id_key(self):
        """装备仍在栏位（item_id 键）→ 正常发布；TTL 取 skill cooldown."""
        latest = _game_state(items=[{"item_id": 3157, "slot": 0}])
        fake = FakeRedisStore(state=latest)
        out = await _nodes(fake).publish(_coach_state(
            "item_purchased", event_data={"item_id": 3157},
            game_state=latest, skill_name="build"))
        assert out["tip"] is not None
        assert out["tip"]["skill"] == "build"
        # build skill 在 SKILL.md 中声明 cooldown: 30
        assert fake.marked == [("build", 30)]

    @pytest.mark.asyncio
    async def test_item_gone_cancels_tip(self):
        """装备已不在栏位 → 建议取消."""
        latest = _game_state(items=[{"item_id": 1001, "slot": 0}])
        fake = FakeRedisStore(state=latest)
        out = await _nodes(fake).publish(_coach_state(
            "item_purchased", event_data={"item_id": 3157},
            game_state=latest, skill_name="build"))
        assert out.get("tip") is None
        assert out["skip_reason"] == "items_gone"

    @pytest.mark.asyncio
    async def test_zero_cooldown_skill_skips_dedup_mark(self):
        """review 声明 cooldown: 0 → 不进去重表（0 不能被 or 吞成默认 120s）."""
        fake = FakeRedisStore(state=None)
        out = await _nodes(fake).publish(_coach_state(
            "game_end", game_state=_game_state(), skill_name="review"))
        assert out["tip"] is not None
        assert fake.marked == []


# ── route_skill ───────────────────────────────────────

class TestRouteSkill:
    def test_low_health_message_carries_real_hp_pct(self):
        """B4 回归：route_skill 必须把真实 game_state 传给 planner，
        而不是 None —— 否则消息里的 HP% 永远缺失."""
        from planner.planner import Planner

        nodes = _nodes()
        nodes.deps.planner = Planner()
        gs = _game_state(hp=200, max_hp=1000)
        out = nodes.route_skill(_coach_state("low_health", game_state=gs))
        assert out["skill_name"] == "survival"
        assert "20% HP" in out["skill_message"]

    def test_invalid_game_state_falls_back_to_none(self):
        """game_state 无法解析为 GameState 时不炸，退回无状态消息."""
        from planner.planner import Planner

        nodes = _nodes()
        nodes.deps.planner = Planner()
        out = nodes.route_skill(_coach_state("low_health", game_state="not-a-dict"))
        assert out["skill_name"] == "survival"
        assert "HP" not in out["skill_message"]
