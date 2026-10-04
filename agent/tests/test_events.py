"""services/events.py 事件分类 / LCU 上下文 / 会话重置测试."""

from types import SimpleNamespace

import pytest

from models.state import CoachEvent, GameState
from services import events


def _ctx():
    return SimpleNamespace(
        redis_store=SimpleNamespace(reset_calls=0),
        memory=SimpleNamespace(
            user=SimpleNamespace(top_of_mind=["上局残留"], context={
                "runes": {"perk_ids": [1]}, "current_zone": "河道",
                "summoner_name": "旧名", "assigned_position": "MID",
            }),
        ),
        metrics={},
    )


class _ResetRecorder:
    def __init__(self):
        self.calls = 0

    async def reset_session(self, session_id):
        self.calls += 1


def _dead_state() -> GameState:
    return GameState.model_validate({
        "game_time": 900.0,
        "active_player": {"health": 0, "max_health": 1000},
    })


def test_death_event_passes_dead_filter():
    """P0 回归：death 必须穿透死亡过滤（survival 复活期建议的触发源）."""
    assert events.should_skip_dead_event(_dead_state(), CoachEvent(name="death", data={})) is False


def test_other_events_still_filtered_when_dead():
    state = _dead_state()
    assert events.should_skip_dead_event(state, CoachEvent(name="kill", data={})) is True
    assert events.should_skip_dead_event(state, CoachEvent(name="low_health", data={})) is True
    assert events.should_skip_dead_event(state, CoachEvent(name="dragon_soon", data={})) is False


@pytest.mark.asyncio
async def test_lcu_game_start_resets_session_state():
    """P2 回归：新一局开始必须清掉上一局的 tip 冷却/置信度残留，
    否则新局前几分钟的建议被静音（conf:* TTL 24h）."""
    ctx = _ctx()
    recorder = _ResetRecorder()
    ctx.redis_store = SimpleNamespace(reset_session=recorder.reset_session)

    consumed = await events.handle_lcu_event(
        ctx, CoachEvent(name="lcu_game_start", data={
            "runes": {"perk_ids": [8112, 8143]},
            "top_masteries": [{"champion_id": 103}, {"champion_id": 1}],
            "summoner_name": "新名",
        }))

    assert consumed is True
    assert recorder.calls == 1
    assert ctx.memory.user.top_of_mind == []
    assert "current_zone" not in ctx.memory.user.context
    assert "assigned_position" not in ctx.memory.user.context
    # 新局的上下文已写入
    assert ctx.memory.user.context["runes"] == {"perk_ids": [8112, 8143]}
    assert ctx.memory.user.context["summoner_name"] == "新名"
    assert ctx.metrics["session_resets"] == 1


@pytest.mark.asyncio
async def test_lcu_other_events_do_not_reset():
    ctx = _ctx()
    recorder = _ResetRecorder()
    ctx.redis_store = SimpleNamespace(reset_session=recorder.reset_session)

    assert await events.handle_lcu_event(
        ctx, CoachEvent(name="lcu_champion_picked", data={"champion_id": 103})) is True
    assert await events.handle_lcu_event(
        ctx, CoachEvent(name="gameflow_phase_change", data={})) is True
    assert recorder.calls == 0
    assert ctx.memory.user.top_of_mind == ["上局残留"]
