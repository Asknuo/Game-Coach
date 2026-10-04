"""planner/planner.py 路由与消息构造测试."""

from models.state import CoachEvent, GameState
from planner.planner import Planner, _num


def test_num_coerces_valid_values():
    assert _num({"delta": 1500}, "delta", 0) == 1500.0
    assert _num({"delta": "1500"}, "delta", 0) == 1500.0
    assert _num({"seconds_left": 12.8}, "seconds_left", 30) == 12.8


def test_num_falls_back_on_garbage():
    """P2 回归：event.data 来自 Go map[string]interface{}，零校验时一个
    字符串字段就能让 :.0f 格式化炸掉整条图（route_skill 抛 → tip 全丢）."""
    assert _num({"delta": None}, "delta", 0) == 0.0
    assert _num({"delta": "abc"}, "delta", 7) == 7.0
    assert _num({}, "delta", 7) == 7.0


def test_plan_survives_garbage_event_data():
    """整条 plan() 面对畸形 data 不能抛异常——降级为通用文案即可."""
    tip = Planner().plan(CoachEvent(name="gold_spike", data={"delta": "not-a-number"}), None)
    assert tip is not None
    assert "经济突增" in tip.message  # 默认值 0 兜底，未崩溃

    tip = Planner().plan(CoachEvent(name="dragon_soon", data={"seconds_left": None}), None)
    assert tip is not None and "龙" in tip.message


def test_plan_maps_events_to_skills():
    assert Planner().plan(CoachEvent(name="low_health", data={}), None).skill == "survival"
    assert Planner().plan(CoachEvent(name="dragon_soon", data={}), None).skill == "dragon"
    assert Planner().plan(CoachEvent(name="game_end", data={}), None).skill == "review"
    assert Planner().plan(CoachEvent(name="不存在的事件", data={}), None) is None


def test_plan_uses_game_state_hp_in_message():
    gs = GameState.model_validate({
        "game_time": 300.0,
        "active_player": {"health": 180, "max_health": 1000},
    })
    tip = Planner().plan(CoachEvent(name="low_health", data={}), gs)
    assert tip is not None
    assert "18% HP" in tip.message
