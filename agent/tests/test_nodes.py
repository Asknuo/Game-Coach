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
        "tip_id": "",
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

    def test_death_event_passes_death_filter(self):
        """回归：death 自身必须穿透死亡过滤——survival skill 的复活期
        建议依赖它（SKILL.md events 声明），曾被 session/图内双重吞掉."""
        gs = _game_state(hp=0, max_hp=1000)
        out = _nodes().detect_signals(_coach_state("death", game_state=gs))
        assert out["is_valid"] is True
        assert "player_died" in out["signals"]

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

    @pytest.mark.asyncio
    async def test_reject_emits_tip_cancel(self):
        """回归：validate 拒发必须撤回流式卡片（tip_id 关联）."""
        cancelled: list[tuple[str, str]] = []

        async def cancel(tip_id, skill):
            cancelled.append((tip_id, skill))

        nodes = GraphNodes(GraphDeps(redis_store=FakeRedisStore(confidence=0.6),
                                     on_tip_cancel=cancel))
        out = await nodes.validate(_coach_state("low_health", skill_name="survival",
                                                tip_id="test:low_health:1"))
        assert out["should_publish"] is False
        assert cancelled and cancelled[0][1] == "survival"

    @pytest.mark.asyncio
    async def test_pass_emits_no_cancel(self):
        nodes = GraphNodes(GraphDeps(redis_store=FakeRedisStore(),
                                     on_tip_cancel=_boom_cancel))
        out = await nodes.validate(_coach_state("low_health"))
        assert out["should_publish"] is True


async def _boom_cancel(tip_id, skill):
    raise AssertionError("放行时不应触发撤回")


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


# ── llm_polish（非流式降级 / 流式增量推送） ─────────────

from types import SimpleNamespace  # noqa: E402


class FakeStreamLLM:
    """流式桩：is_available 真值，apolish_stream 逐块吐给定增量."""

    def __init__(self, chunks):
        self._chunks = chunks

    def is_available(self):
        return True

    async def apolish_stream(self, tip, snapshot, rag_context=None):
        for c in self._chunks:
            yield c


class TestLLMPolish:
    @pytest.mark.asyncio
    async def test_no_llm_falls_back_to_draft(self):
        """无 LLM 依赖 → 直接用草稿，不炸."""
        out = await _nodes().llm_polish(_coach_state("low_health"))
        assert out["polished_message"] == "draft"

    @pytest.mark.asyncio
    async def test_non_stream_uses_apolish_result(self):
        """未注入 emitter → 走非流式 apolish，取返回 message（与原行为一致）."""

        class FakePolishLLM:
            def is_available(self):
                return True

            async def apolish(self, tip, snapshot, rag_context=None):
                return SimpleNamespace(message="润色结果")

        nodes = GraphNodes(GraphDeps(llm=FakePolishLLM()))
        out = await nodes.llm_polish(_coach_state("low_health"))
        assert out["polished_message"] == "润色结果"

    @pytest.mark.asyncio
    async def test_stream_emits_accumulated_text(self):
        """流式分支：首块即时推送，结束推全文，polished_message 为拼接结果."""
        emitted: list[tuple[str, str]] = []

        async def emitter(tip, text, tip_id):
            emitted.append((text, tip_id))

        nodes = GraphNodes(GraphDeps(
            llm=FakeStreamLLM(["快撤", "回城补给", "再上线"]),
            on_polish_delta=emitter,
        ))
        out = await nodes.llm_polish(_coach_state("low_health"))

        assert out["polished_message"] == "快撤回城补给再上线"
        assert emitted[0][0] == "快撤"                 # 首块不等节流，overlay 立即可见
        assert emitted[-1][0] == "快撤回城补给再上线"    # 落锤前推完整文本
        # 同一 tip_id 贯穿全部增量与状态，供撤回/关联
        assert out["tip_id"]
        assert all(tid == out["tip_id"] for _, tid in emitted)

    @pytest.mark.asyncio
    async def test_stream_empty_falls_back_to_draft(self):
        """流式无任何输出 → 回退草稿，不做推送."""
        emitted: list[str] = []

        async def emitter(tip, text, tip_id):
            emitted.append(text)

        nodes = GraphNodes(GraphDeps(llm=FakeStreamLLM([]), on_polish_delta=emitter))
        out = await nodes.llm_polish(_coach_state("low_health"))
        assert out["polished_message"] == "draft"
        assert emitted == []

    @pytest.mark.asyncio
    async def test_emitter_failure_does_not_break_polish(self):
        """overlay 推送失败不中断润色——文本仍然完整产出."""

        async def bad_emitter(tip, text, tip_id):
            raise RuntimeError("overlay gone")

        nodes = GraphNodes(GraphDeps(
            llm=FakeStreamLLM(["块1", "块2"]), on_polish_delta=bad_emitter))
        out = await nodes.llm_polish(_coach_state("low_health"))
        assert out["polished_message"] == "块1块2"

    @pytest.mark.asyncio
    async def test_stream_mid_failure_keeps_partial_text(self):
        """流中断已产出部分文本 → 保留部分（与 overlay 已显示内容一致），不回退草稿."""

        class InterruptedStreamLLM(FakeStreamLLM):
            async def apolish_stream(self, tip, snapshot, rag_context=None):
                for c in self._chunks:
                    yield c
                raise ConnectionError("stream reset")

        emitted: list[str] = []

        async def emitter(tip, text, tip_id):
            emitted.append(text)

        nodes = GraphNodes(GraphDeps(
            llm=InterruptedStreamLLM(["先撤", "再打"]), on_polish_delta=emitter))
        out = await nodes.llm_polish(_coach_state("low_health"))
        assert out["polished_message"] == "先撤再打"
        assert emitted[-1] == "先撤再打"  # 最终 emit 与落锤文本一致


# ── 四层上下文组装（分层优先级 + token 预算） ────────────

from prompt.context_builder import build_polish_context, estimate_tokens  # noqa: E402


class TestBuildPolishContext:
    def test_empty_returns_none(self):
        """四层全空 → 不注入任何上下文."""
        assert build_polish_context() is None
        assert build_polish_context(skill_context="   ", rag_docs=[]) is None

    def test_display_order_is_guidelines_first(self):
        """展示顺序与裁剪优先级无关：指导方针 → 坑点 → 知识 → 记忆."""
        ctx = build_polish_context(
            skill_context="## S\n方针", skill_gotchas="坑点",
            rag_docs=["知识"], memory_context="记忆",
        )
        assert (
            ctx.index("Coaching Guidelines")
            < ctx.index("CRITICAL Gotchas")
            < ctx.index("Game Knowledge")
            < ctx.index("Player Context")
        )

    def test_budget_trims_guidelines_before_rag(self):
        """预算不足 → 先裁指导方针（最低优先级），保留 RAG 知识."""
        guide = "## S1\n" + "字" * 4000
        ctx = build_polish_context(
            skill_context=guide, rag_docs=["关键的局势知识"], budget_tokens=60,
        )
        assert "S1" not in ctx                 # 指导方针被裁掉
        assert "关键的局势知识" in ctx          # RAG 保留

    def test_gotchas_and_memory_never_trimmed(self):
        """坑点与记忆是最高信号/个性化，任何预算下都不裁剪."""
        ctx = build_polish_context(
            skill_gotchas="绝对不能给错建议", memory_context="玩家是钻石玩家",
            skill_context="## S1\n" + "字" * 5000, budget_tokens=5,
        )
        assert "绝对不能给错建议" in ctx
        assert "玩家是钻石玩家" in ctx
        assert "S1" not in ctx

    def test_truncation_stops_at_section_boundary(self):
        """按整节裁剪，不产生半句；保留预算装得下的前若干节."""
        guide = "## A\n" + "字" * 20 + "\n## B\n" + "字" * 4000
        ctx = build_polish_context(skill_context=guide, budget_tokens=40)
        assert "## A" in ctx
        assert "## B" not in ctx               # 第二节整节丢弃，而非拦腰截断

    def test_estimate_tokens_cjk_heavier_than_ascii(self):
        """中文按 1 token/字、英文按 4 char/token 粗估."""
        assert estimate_tokens("英雄联盟") == 4
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("") == 0
