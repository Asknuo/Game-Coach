"""图级别集成测试：编译后的 coaching graph 端到端跑通条件路由.

test_nodes 只测单节点；这里覆盖 Builder 的边与跨节点数据流——
validate 拒发 → _cancel_stream → tip_cancel 回调的完整链路。
"""

import pytest

from graph import GraphDeps, build_coaching_graph
from graph.state import build_initial_state
from models.state import CoachEvent, GameState
from planner.planner import Planner
from tests.test_nodes import FakeRedisStore, FakeStreamLLM


def _low_health_event() -> CoachEvent:
    return CoachEvent(name="low_health", data={"health_pct": 12, "game_time": 300})


def _snapshot() -> GameState:
    return GameState.model_validate({
        "game_time": 300.0,
        "active_player": {"health": 120, "max_health": 1000, "team": "CHAOS",
                          "position": {"x": 5000, "y": 5000}},
    })


def _graph(**deps):
    # planner 是路由必需依赖（生产环境总有）；缺失的其余依赖走降级路径
    deps.setdefault("planner", Planner())
    return build_coaching_graph(GraphDeps(**deps))


@pytest.mark.asyncio
async def test_graph_publishes_tip_with_tip_id():
    """全链路放行：tip payload 必须带 tip_id（客户端卡片关联的锚）."""
    graph = _graph(redis_store=FakeRedisStore())
    state = build_initial_state(_low_health_event(), _snapshot(), [], 3)

    out = await graph.ainvoke(state)

    assert out["should_publish"] is True
    assert out["tip"]["skill"] == "survival"
    assert out["tip"]["tip_id"] == out["tip_id"]


@pytest.mark.asyncio
async def test_graph_cancels_stream_when_muted():
    """P0 回归：validate 因低置信度拒发 → 必须触发 tip_cancel，
    否则 overlay 上早于 validate 的流式卡片变成永不留锤的孤儿."""
    cancelled: list[tuple[str, str]] = []

    async def on_cancel(tip_id, skill):
        cancelled.append((tip_id, skill))

    graph = _graph(redis_store=FakeRedisStore(confidence=0.6),
                   llm=FakeStreamLLM([]),  # 可用但无输出：tip_id 已生成
                   on_tip_cancel=on_cancel)
    state = build_initial_state(_low_health_event(), _snapshot(), [], 3)

    out = await graph.ainvoke(state)

    assert out["should_publish"] is False
    assert out["skip_reason"] == "low_confidence"
    assert len(cancelled) == 1
    assert cancelled[0][1] == "survival"


@pytest.mark.asyncio
async def test_graph_no_skill_event_ends_early():
    """未知事件 → route_skill 判无效 → END，无 tip 也无取消."""
    cancelled: list[tuple[str, str]] = []

    async def on_cancel(tip_id, skill):
        cancelled.append((tip_id, skill))

    graph = _graph(on_tip_cancel=on_cancel)
    state = build_initial_state(CoachEvent(name="不存在的事件", data={}), None, [], 1)

    out = await graph.ainvoke(state)

    assert out.get("tip") is None
    assert cancelled == []  # 没有 tip_id 可撤（润色节点从未执行）
