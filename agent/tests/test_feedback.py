"""memory/redis_store.py 反馈闭环状态机测试（内存版 FakeRedis，不连真实 Redis）."""

import time

import pytest

from memory.redis_store import ADVICE_FEEDBACK_WINDOW, RedisStore


class FakeRedisClient:
    """实现 RedisStore 用到的最小异步 Redis 接口."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def exists(self, key):
        return key in self.data


def _store() -> RedisStore:
    store = RedisStore(url="redis://unused:0/0")
    store._client = FakeRedisClient()
    return store


def _state(hp=100.0, max_hp=1000.0, items=None):
    return {
        "active_player": {
            "health": hp,
            "max_health": max_hp,
            "items": items or [],
        }
    }


@pytest.mark.asyncio
async def test_pending_within_window():
    """建议刚发出时，未观察到采纳 → pending（记录保留，下帧继续检查）."""
    store = _store()
    await store.record_advice_given(
        "s", skill="survival", event_name="low_health",
        context={"health_pct": 20},
    )
    status, skill, _ = await store.check_advice_followed("s", _state(hp=200))
    assert status == "pending"
    assert skill == "survival"
    # pending 不消费记录 → 下一帧仍可检查
    status2, _, _ = await store.check_advice_followed("s", _state(hp=200))
    assert status2 == "pending"


@pytest.mark.asyncio
async def test_followed_when_hp_recovered():
    """HP 恢复 30%+ → followed，记录被消费."""
    store = _store()
    await store.record_advice_given(
        "s", skill="survival", event_name="low_health",
        context={"health_pct": 20},
    )
    status, skill, reason = await store.check_advice_followed("s", _state(hp=900))
    assert status == "followed"
    assert skill == "survival"
    assert "hp_recovered" in reason
    # 已消费 → 再次检查应无建议
    status2, _, _ = await store.check_advice_followed("s", _state(hp=900))
    assert status2 == "no_advice"


@pytest.mark.asyncio
async def test_expired_after_window():
    """超过观察窗口未采纳 → skipped（三态语义下 build 不可客观判定时中性跳过）."""
    store = _store()
    await store.record_advice_given(
        "s", skill="build", event_name="item_purchased", context={"item_count": 2},
    )
    # 手动把建议时间戳拨回窗口之前
    raw = store._client.data["coach:s:last_advice"]
    import json
    advice = json.loads(raw)
    advice["ts"] = time.time() - ADVICE_FEEDBACK_WINDOW - 1
    store._client.data["coach:s:last_advice"] = json.dumps(advice)

    status, skill, _ = await store.check_advice_followed("s", _state())
    assert status == "skipped"
    assert skill == "build"
    status2, _, _ = await store.check_advice_followed("s", _state())
    assert status2 == "no_advice"


def _enemy_state(enemy_name="EnemyMid", kills=3, deaths=0):
    """构造含指定敌方玩家的 state（用于 enemy_fed / enemy_gold_lead 判定）."""
    return {
        "active_player": {"health": 1000.0, "max_health": 1000.0, "items": []},
        "all_players": [
            {"summoner_name": enemy_name, "kills": kills, "deaths": deaths},
        ],
    }


@pytest.mark.asyncio
async def test_enemy_fed_followed_on_shutdown():
    """enemy_fed 建议发出后目标敌人死亡 → followed（已 shut down）."""
    store = _store()
    await store.record_advice_given(
        "s", skill="survival", event_name="enemy_fed",
        context={"enemy_name": "EnemyMid", "enemy_deaths": 0, "enemy_kills": 3},
    )
    status, skill, reason = await store.check_advice_followed(
        "s", _enemy_state(kills=3, deaths=1),
    )
    assert status == "followed"
    assert reason == "enemy_shutdown"


@pytest.mark.asyncio
async def test_enemy_fed_not_followed_on_threat_growing():
    """enemy_fed 建议发出后目标继续拿头 → not_followed（唯一会降置信度的分支）."""
    store = _store()
    await store.record_advice_given(
        "s", skill="survival", event_name="enemy_fed",
        context={"enemy_name": "EnemyMid", "enemy_deaths": 0, "enemy_kills": 3},
    )
    status, skill, reason = await store.check_advice_followed(
        "s", _enemy_state(kills=5, deaths=0),
    )
    assert status == "not_followed"
    assert reason == "threat_growing"


@pytest.mark.asyncio
async def test_enemy_fed_pending_while_observing():
    """enemy_fed 目标无变化 → pending（继续观察）."""
    store = _store()
    await store.record_advice_given(
        "s", skill="survival", event_name="enemy_fed",
        context={"enemy_name": "EnemyMid", "enemy_deaths": 0, "enemy_kills": 3},
    )
    status, _, _ = await store.check_advice_followed("s", _enemy_state())
    assert status == "pending"


@pytest.mark.asyncio
async def test_macro_check_skipped_neutral():
    """战略类事件（macro_check）不可客观判定 → 超时后 skipped，置信度不受罚."""
    store = _store()
    await store.record_advice_given(
        "s", skill="macro", event_name="macro_check", context={},
    )
    raw = store._client.data["coach:s:last_advice"]
    import json
    advice = json.loads(raw)
    advice["ts"] = time.time() - ADVICE_FEEDBACK_WINDOW - 1
    store._client.data["coach:s:last_advice"] = json.dumps(advice)

    status, skill, _ = await store.check_advice_followed("s", _state())
    assert status == "skipped"
    assert skill == "macro"
    # 关键断言：置信度未被扣减（这是修复"只罚不赏"的核心语义）
    assert await store.get_skill_confidence("s", "macro") == 1.0


@pytest.mark.asyncio
async def test_confidence_adjustment_bounds():
    """置信度按采纳 +/- 调整，并限制在 0.5~1.5."""
    store = _store()
    assert await store.get_skill_confidence("s", "survival") == 1.0

    val = await store.adjust_skill_confidence("s", "survival", True)
    assert val == pytest.approx(1.05)
    val = await store.adjust_skill_confidence("s", "survival", False)
    assert val == pytest.approx(1.02)

    # 连续不采纳，下限 0.5
    for _ in range(30):
        val = await store.adjust_skill_confidence("s", "survival", False)
    assert val == 0.5

    # 空 skill 名不写入（回归：历史上曾把反馈写到空 key 上）
    assert await store.adjust_skill_confidence("s", "", False) == 1.0


@pytest.mark.asyncio
async def test_tip_dedup_with_custom_ttl():
    store = _store()
    assert not await store.was_tip_recently_sent("s", "build")
    await store.mark_tip_sent("s", "build", ttl=30)
    assert await store.was_tip_recently_sent("s", "build")
    assert not await store.was_tip_recently_sent("s", "dragon")
