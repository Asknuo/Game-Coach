"""LangGraph 节点函数 — coaching 流水线每一步的具体逻辑."""

import asyncio
import logging

from graph.state import CoachState
from memory.redis_store import DEFAULT_TIP_TTL, MIN_CONFIDENCE_TO_PUBLISH

logger = logging.getLogger(__name__)

# ── 不可序列化的模块级单例，通过闭包注入 ──
_injections: dict[str, object] = {}


def set_injections(
    planner,
    llm,
    retriever,
    injector,
    redis_store,
    memory=None,
):
    """注入模块级依赖（避免 LangGraph checkpoint 序列化问题）."""
    _injections["planner"] = planner
    _injections["llm"] = llm
    _injections["retriever"] = retriever
    _injections["injector"] = injector
    _injections["redis_store"] = redis_store
    _injections["memory"] = memory


# ── 解析事件 ──────────────────────────────────────────

def parse_event(state: CoachState) -> CoachState:
    """解析原始事件，提取 event_name / event_data."""
    event = state.get("event", {})
    event_name = event.get("name", "")
    event_data = event.get("data", {})

    logger.debug("parse_event: %s", event_name)
    return {
        **state,
        "event_name": event_name,
        "event_data": event_data,
        "is_valid": bool(event_name),
        "signals": [],
        # priority 保留上游（队列/紧急通道）计算值，detect_signals 再按事件上调
        "skill_name": "",
        "skill_message": "",
        "rag_query": "",
        "rag_docs": [],
        "memory_context": "",
        "skill_context": "",     # SKILL.md 正文
        "skill_gotchas": "",     # 坑点清单
        "polished_message": "",
        "should_publish": False,
        "skip_reason": "",
        "tip": None,
    }


# ── 信号检测 ──────────────────────────────────────────

# 死亡时仍需推送的目标类事件
_OBJECTIVE_EVENTS = ("dragon_soon", "baron_soon")

# 静态信号表：事件名 → (优先级下限, 附加信号)
_SIGNAL_TABLE: dict[str, tuple[int, list[str]]] = {
    "dragon_soon": (2, ["objective_stage"]),
    "baron_soon": (2, ["objective_stage"]),
    "item_purchased": (1, ["power_spike"]),
    "item_upgraded": (1, ["power_spike"]),
    "gold_spike": (1, ["power_spike"]),
    "kill": (2, ["kill_secured"]),
    "death": (2, ["player_died"]),
    "teamfight_detected": (2, ["teamfight"]),
    "enemy_gold_lead": (2, ["enemy_power_spike", "danger"]),
    "enemy_fed": (3, ["enemy_fed", "danger"]),
    "enemy_item_purchased": (1, ["enemy_power_spike"]),
}


def _hp_pct(active: dict) -> float:
    """活跃玩家的血量百分比（max_hp<=0 时视为满血）."""
    hp = active.get("health", 1)
    max_hp = active.get("max_health", 1)
    return hp / max_hp * 100 if max_hp > 0 else 100


def _is_in_fountain(active: dict) -> bool:
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


def detect_signals(state: CoachState) -> CoachState:
    """清洗无效事件 + 检测关键信号 + 计算优先级."""
    name = state["event_name"]
    data = state["event_data"]
    gs = state.get("game_state", {})

    # 上游（_event_priority / SKILL_REGISTRY）已给出基础优先级，这里只做上调
    priority = state.get("priority", 1) or 1

    active = gs.get("active_player", {}) if gs else {}
    hp_pct = _hp_pct(active)

    # 死亡时跳过非龙/大龙事件
    if hp_pct == 0 and name not in _OBJECTIVE_EVENTS:
        logger.debug("detect_signals: skip (dead) %s", name)
        return {**state, "is_valid": False, "skip_reason": "player_dead"}

    if name == "low_health":
        # 上下文抑制：在泉水附近说明已回城/回复中，不发
        if _is_in_fountain(active):
            logger.debug("detect_signals: skip low_health (in fountain)")
            return {**state, "is_valid": False, "skip_reason": "in_fountain"}
        priority = max(priority, 3)
        signals = ["low_health"]
        if hp_pct < 15:
            signals.append("critically_low")
    else:
        floor, base_signals = _SIGNAL_TABLE.get(name, (1, []))
        signals = list(base_signals)
        priority = max(priority, floor)
        # 目标临近：10s 内出生 → 提到最高优先级
        if name in _OBJECTIVE_EVENTS and data.get("seconds_left", 99) <= 10:
            signals.append("imminent_objective")
            priority = 3

    logger.debug("detect_signals: %s signals=%s priority=%d", name, signals, priority)
    return {**state, "signals": signals, "priority": priority, "is_valid": True}


# ── 路由到 Skill ──────────────────────────────────────

def _rag_query_enemy_item(state: CoachState, resolver) -> list[str]:
    """enemy_item_purchased：翻译 itemID→名称并写回 event_data，返回查询片段."""
    event_data = state.get("event_data", {})
    enemy_champ = event_data.get("enemy_champion", "")
    enemy_name = event_data.get("enemy_name", "")
    item_ids = event_data.get("item_ids", [])

    # 翻译 itemID → 物品名称
    item_names = resolver.get_names(item_ids)
    item_descs = [resolver.describe_item(iid) for iid in item_ids]

    # 把名称写回 event_data，后续节点（inject_memory、llm_polish）可直接用
    state["event_data"]["item_names"] = item_names
    state["event_data"]["item_descs"] = item_descs

    parts: list[str] = []
    if enemy_champ:
        parts.append(f"enemy {enemy_champ} {enemy_name} purchased {' '.join(item_names)} counter build counter items")
    if item_names:
        parts.append(f"items {' '.join(item_names)} counter")
    return parts


def _build_rag_query(state: CoachState) -> str:
    """按事件类型拼接 RAG 检索查询串（skill message + 事件上下文）."""
    from knowledge.item_resolver import get_item_resolver

    event_name = state["event_name"]
    event_data = state.get("event_data", {})
    parts = [state["skill_message"]]

    if event_name == "dragon_soon":
        parts.append("dragon fight positioning objective strategy")
    elif event_name == "baron_soon":
        parts.append("baron fight positioning objective strategy")
    elif event_name == "low_health":
        parts.append("when low health recall sustain laning recovery")
    elif event_name == "item_purchased":
        parts.append("recommended next items build order")
    elif event_name == "item_sold":
        name = get_item_resolver().get_name(event_data.get("item_id", 0))
        parts.append(f"sold {name} freed inventory slot next item build order")
    elif event_name == "item_upgraded":
        resolver = get_item_resolver()
        old_name = resolver.get_name(event_data.get("old_item_id", 0))
        new_name = resolver.get_name(event_data.get("new_item_id", 0))
        parts.append(f"upgraded {old_name} to {new_name} completed item powerspike")
    elif event_name == "enemy_item_purchased":
        parts.extend(_rag_query_enemy_item(state, get_item_resolver()))
    elif event_name == "kill":
        kills = event_data.get("total_kills", 1)
        parts.append(f"after getting kill {kills} what to do objective push tower dragon capitalize advantage")
    elif event_name == "enemy_gold_lead":
        enemy_champ = event_data.get("enemy_champion", "")
        gap = event_data.get("gold_gap", 0)
        parts.append(f"enemy {enemy_champ} has {gap:.0f} gold lead how to play from behind counter fed enemy")
    elif event_name == "enemy_fed":
        enemy_champ = event_data.get("enemy_champion", "")
        kills = event_data.get("kills", 0)
        parts.append(f"enemy {enemy_champ} fed {kills} kills how to shut down shutdown target counter")
    else:
        parts.append("strategy tips priority")

    return " ".join(parts)


def route_skill(state: CoachState) -> CoachState:
    """事件名 → Skill，加载 SKILL.md 上下文和坑点清单."""
    planner = _injections["planner"]

    from models.state import CoachEvent
    from planner.planner import get_skill_context, get_skill_gotchas

    event = CoachEvent(
        name=state["event_name"],
        data=state["event_data"],
    )
    tip = planner.plan(event, None)

    if not tip:
        logger.debug("route_skill: %s → no skill matched", state["event_name"])
        return {**state, "is_valid": False, "skip_reason": "no_skill"}

    skill_name = tip.skill

    # ── 加载 SKILL.md 正文和坑点清单 ──
    skill_context = get_skill_context(skill_name)
    skill_gotchas = get_skill_gotchas(skill_name)

    state["skill_name"] = skill_name
    state["skill_message"] = tip.message
    state["skill_context"] = skill_context
    state["skill_gotchas"] = skill_gotchas

    logger.debug(
        "route_skill: %s → %s (context: %d chars, gotchas: %d chars)",
        state["event_name"], skill_name,
        len(skill_context), len(skill_gotchas),
    )

    state["rag_query"] = _build_rag_query(state)
    return state


# ── 向量检索 ──────────────────────────────────────────

def _find_nearest_enemy_champion(
    all_players: list[dict], active_team: str, active_x: float, active_y: float,
) -> str:
    """返回距离活跃玩家最近（<5000）的敌方英雄名，无则空串."""
    from models.state import is_position_valid

    best_dist = float("inf")
    nearest = ""
    for p in all_players:
        if not p.get("team") or p["team"] == active_team:
            continue
        enemy_pos = p.get("position", {})
        # 坐标有效性校验：排除 (0,0) 等无效坐标
        if not isinstance(enemy_pos, dict) or not is_position_valid(enemy_pos):
            continue
        ex = enemy_pos.get("x", 0)
        ey = enemy_pos.get("y", 0)
        dist = ((active_x - ex) ** 2 + (active_y - ey) ** 2) ** 0.5
        if dist < best_dist and dist < 5000:
            best_dist = dist
            nearest = p.get("champion_name", "")
    return nearest


def _resolve_champions(
    gs: dict, event_name: str, enemy_champion: str,
) -> tuple[str, str, float]:
    """从 game_state 解析 (己方英雄名, 敌方英雄名, 游戏时间).

    RAG 检索必须用英雄名（如 "Ahri"），不能用召唤师名；
    active_player.champion_name 由 sync_active_player 补全.
    """
    active_player = gs.get("active_player", {})
    summoner = active_player.get("summoner_name", "")
    champion = active_player.get("champion_name", "")

    all_players: list[dict] = gs.get("all_players", [])
    active_team = ""
    active_pos = active_player.get("position", {})
    for p in all_players:
        if p.get("summoner_name") == summoner:
            active_team = p.get("team", "")
            active_pos = p.get("position", active_pos)
            if not champion:
                champion = p.get("champion_name", "")
            break

    # 就近取敌方英雄用于克制攻略（enemy_item_purchased 已有明确敌方，跳过）
    if active_team and active_pos and event_name != "enemy_item_purchased":
        active_x = active_pos.get("x", 0) if isinstance(active_pos, dict) else 0
        active_y = active_pos.get("y", 0) if isinstance(active_pos, dict) else 0
        nearest = _find_nearest_enemy_champion(
            all_players, active_team, active_x, active_y,
        )
        if nearest:
            enemy_champion = nearest

    return champion, enemy_champion, gs.get("game_time", 0)


async def retrieve_knowledge(state: CoachState) -> CoachState:
    """ChromaDB RAG 检索 — 聚合己方+敌方英雄攻略、游戏机制等多源知识."""
    retriever = _injections.get("retriever")
    if not retriever:
        return state

    gs = state.get("game_state", {})
    event_name = state.get("event_name", "")

    # ★ enemy events: 直接用事件数据中的敌方英雄（Go 侧填的是 ChampionName）
    enemy_champion = ""
    if event_name in ("enemy_item_purchased", "enemy_gold_lead", "enemy_fed"):
        enemy_champion = state.get("event_data", {}).get("enemy_champion", "")

    champion = ""
    game_time = 0.0
    if gs:
        champion, enemy_champion, game_time = _resolve_champions(
            gs, event_name, enemy_champion,
        )

    rag_query = state.get("rag_query", "")

    # embedding + ChromaDB 查询是同步 IO，offload 到线程池避免阻塞事件循环
    aggregated = await asyncio.to_thread(
        retriever.aggregate_coaching_context,
        ally_champion=champion,
        enemy_champion=enemy_champion if enemy_champion != champion else None,
        game_time=game_time,
        event_name=event_name,
        event_query=rag_query,
    )

    state["rag_docs"] = [aggregated] if aggregated else []
    logger.debug("retrieve: %d aggregated docs for %s", len(state["rag_docs"]), event_name)
    return state


# ── 记忆注入 ──────────────────────────────────────────

def inject_memory(state: CoachState) -> CoachState:
    """格式化 PlayerMemory 为 LLM 上下文."""
    injector = _injections.get("injector")
    if not injector:
        return state

    # memory 由 app.py 通过 set_injections 注入（避免 import app 循环依赖）
    memory = _injections.get("memory")
    if memory is None:
        return state

    ctx = injector.format(memory, token_budget=200)
    logger.debug("inject_memory: %d chars", len(ctx))
    return {**state, "memory_context": ctx}


# ── LLM 润色 ──────────────────────────────────────────

async def llm_polish(state: CoachState) -> CoachState:
    """调用 LLM 润色教练建议，注入 SKILL.md 上下文 + 坑点清单."""
    llm = _injections.get("llm")
    if not llm or not llm._client:
        return {**state, "polished_message": state["skill_message"]}

    from models.state import CoachingTip

    tip = CoachingTip(
        skill=state["skill_name"],
        message=state["skill_message"],
        priority=state["priority"],
    )

    # ── 构建增强上下文：SKILL.md + gotchas + RAG + memory ──
    parts = []

    # 1. SKILL.md 正文（skill 的 coaching 指导方针）
    if state.get("skill_context"):
        parts.append("=== Coaching Guidelines ===\n" + state["skill_context"])

    # 2. 坑点清单（最高信号内容）
    if state.get("skill_gotchas"):
        parts.append("=== CRITICAL Gotchas (do NOT give wrong advice) ===\n" + state["skill_gotchas"])

    # 3. RAG 知识
    if state.get("rag_docs"):
        parts.append("=== Game Knowledge ===\n" + "\n".join(state["rag_docs"][:2]))

    # 4. 记忆
    if state.get("memory_context"):
        parts.append("=== Player Context ===\n" + state["memory_context"])

    rag_ctx = "\n\n".join(parts) if parts else None

    try:
        # ★ 异步链路：不阻塞 FastAPI 事件循环
        result = await llm.apolish(tip, None, rag_context=rag_ctx)
        state["polished_message"] = result.message
        logger.debug("llm_polish: %s", state["polished_message"][:80])
    except Exception:
        logger.exception("llm_polish failed")
        state["polished_message"] = state["skill_message"]

    return state


# ── 验证 / 去重 ──────────────────────────────────────

async def validate(state: CoachState) -> CoachState:
    """去重 + 置信度检查，决定是否发布.

    - 去重窗口 = SKILL.md frontmatter 声明的 cooldown（不再固定 120s）
    - 置信度来自反馈闭环：玩家长期不采纳该 skill 的建议 → 自动降权停发
    """
    redis = _injections.get("redis_store")
    skill = state["skill_name"]

    if not skill:
        return {**state, "should_publish": False, "skip_reason": "no_skill"}

    if redis:
        # ★ 反馈闭环消费：置信度过低的 skill 自动静音
        conf = await redis.get_skill_confidence(state["session_id"], skill)
        if conf < MIN_CONFIDENCE_TO_PUBLISH:
            logger.info("validate: skill=%s muted by low confidence %.2f", skill, conf)
            return {**state, "should_publish": False, "skip_reason": "low_confidence"}

        if await redis.was_tip_recently_sent(state["session_id"], skill):
            logger.debug("validate: duplicate skill=%s", skill)
            return {**state, "should_publish": False, "skip_reason": "duplicate"}

    return {**state, "should_publish": True, "skip_reason": ""}


# ── 发布 ──────────────────────────────────────────────

def _tip_stale_reason(
    event_name: str, event_data: dict, gs: dict, has_latest: bool,
) -> str:
    """时效性检查：返回应跳过发布的原因，空串表示仍然有效."""
    active = gs.get("active_player", {}) if gs else {}
    hp_pct = _hp_pct(active)

    # low_health: 血量已经恢复 > 50% → 取消
    if event_name == "low_health" and hp_pct > 50:
        return "hp_recovered"

    # dragon_soon / baron_soon: 目标已出生（计时器消失）→ 提示已过时
    if event_name == "dragon_soon" and has_latest and not gs.get("dragon_timer"):
        return "objective_gone"
    if event_name == "baron_soon" and has_latest and not gs.get("baron_timer"):
        return "objective_gone"

    # item_purchased: 装备已不在栏位 → 取消
    # （仅检查己方购买；enemy_item_purchased 是敌方装备，与我方栏位无关）
    if event_name == "item_purchased" and active.get("items"):
        bought_id = event_data.get("item_id", 0)
        current_ids = {
            (it.get("item_id") or it.get("itemID", 0))
            for it in active.get("items", [])
        }
        if bought_id and bought_id not in current_ids:
            return "items_gone"

    return ""


async def publish(state: CoachState) -> CoachState:
    """标记 tip 输出，含时效性检查（对比最新 state，而非事件时的旧快照）."""
    event_name = state.get("event_name", "")
    event_data = state.get("event_data", {})

    redis = _injections.get("redis_store")

    # ★ 时效性检查：拉取最新 state — LLM 调用数秒后，事件时的快照可能已过时
    latest = None
    if redis:
        try:
            latest = await redis.get_state(state["session_id"])
        except Exception:
            latest = None
    gs = latest or state.get("game_state", {})

    if gs:
        reason = _tip_stale_reason(event_name, event_data, gs, latest is not None)
        if reason:
            logger.debug("publish: skip %s (%s)", event_name, reason)
            return {**state, "should_publish": False, "skip_reason": reason}

    if redis:
        # ★ 去重 TTL = SKILL.md 声明的 cooldown
        from planner.planner import SKILL_REGISTRY
        meta = SKILL_REGISTRY.get(state["skill_name"], {})
        ttl = int(meta.get("cooldown", DEFAULT_TIP_TTL) or DEFAULT_TIP_TTL)
        await redis.mark_tip_sent(state["session_id"], state["skill_name"], ttl=ttl)

    state["tip"] = {
        "skill": state["skill_name"],
        "message": state["polished_message"],
        "priority": state["priority"],
    }
    logger.info("[%s] %s", state["skill_name"], state["polished_message"][:80])
    return state
