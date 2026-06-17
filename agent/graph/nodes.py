"""LangGraph 节点函数 — coaching 流水线每一步的具体逻辑."""

import logging

from graph.state import CoachState

logger = logging.getLogger(__name__)

# ── 不可序列化的模块级单例，通过闭包注入 ──
_injections: dict[str, object] = {}


def set_injections(
    planner,
    llm,
    retriever,
    injector,
    redis_store,
):
    """注入模块级依赖（避免 LangGraph checkpoint 序列化问题）."""
    _injections["planner"] = planner
    _injections["llm"] = llm
    _injections["retriever"] = retriever
    _injections["injector"] = injector
    _injections["redis_store"] = redis_store


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
        "priority": 1,
        "skill_name": "",
        "skill_message": "",
        "rag_query": "",
        "rag_docs": [],
        "memory_context": "",
        "polished_message": "",
        "should_publish": False,
        "skip_reason": "",
        "tip": None,
    }


# ── 信号检测 ──────────────────────────────────────────

def detect_signals(state: CoachState) -> CoachState:
    """清洗无效事件 + 检测关键信号 + 计算优先级."""
    name = state["event_name"]
    data = state["event_data"]
    gs = state.get("game_state", {})

    signals: list[str] = []
    priority = 1

    # 死亡时跳过低优先级事件
    active = gs.get("active_player", {}) if gs else {}
    hp = active.get("current_health", 1)
    max_hp = active.get("max_health", 1)
    hp_pct = hp / max_hp * 100 if max_hp > 0 else 100

    if hp_pct == 0 and name not in ("dragon_soon", "baron_soon"):
        logger.debug("detect_signals: skip (dead) %s", name)
        return {**state, "is_valid": False, "skip_reason": "player_dead"}

    # ── 信号分类 ──
    if name == "low_health":
        priority = 3
        signals.append("low_health")
        if hp_pct < 15:
            signals.append("critically_low")
    elif name == "dragon_soon":
        priority = 2
        signals.append("objective_stage")
        if data.get("seconds_left", 99) <= 10:
            signals.append("imminent_objective")
            priority = 3
    elif name == "baron_soon":
        priority = 2
        signals.append("objective_stage")
        if data.get("seconds_left", 99) <= 10:
            signals.append("imminent_objective")
            priority = 3
    elif name == "item_purchased":
        priority = 1
        signals.append("power_spike")
    elif name in ("jungle_check", "strategy_check"):
        priority = 1

    logger.debug("detect_signals: %s signals=%s priority=%d", name, signals, priority)
    return {**state, "signals": signals, "priority": priority, "is_valid": True}


# ── 路由到 Skill ──────────────────────────────────────

def route_skill(state: CoachState) -> CoachState:
    """事件名 → Skill 模板生成."""
    planner = _injections["planner"]

    from models.state import CoachEvent

    event = CoachEvent(
        name=state["event_name"],
        data=state["event_data"],
    )
    tip = planner.plan(event, None)
    if tip:
        logger.debug("route_skill: %s → %s", state["event_name"], tip.skill)
        return {
            **state,
            "skill_name": tip.skill,
            "skill_message": tip.message,
        }
    else:
        logger.debug("route_skill: %s → no skill matched", state["event_name"])
        return {**state, "is_valid": False, "skip_reason": "no_skill"}

    # rag_query: skill message + event context 拼接
    query_parts = [state["skill_message"]]
    if state["event_name"] == "dragon_soon":
        query_parts.append("dragon fight positioning objective strategy")
    elif state["event_name"] == "baron_soon":
        query_parts.append("baron fight positioning objective strategy")
    elif state["event_name"] == "low_health":
        query_parts.append("when low health recall sustain laning recovery")
    elif state["event_name"] == "item_purchased":
        query_parts.append("recommended next items build order")
    else:
        query_parts.append("strategy tips priority")

    state["rag_query"] = " ".join(query_parts)
    return state


# ── 向量检索 ──────────────────────────────────────────

def retrieve_knowledge(state: CoachState) -> CoachState:
    """ChromaDB RAG 检索."""
    retriever = _injections.get("retriever")
    if not retriever:
        return state

    champion = ""
    gs = state.get("game_state", {})
    if gs:
        champion = gs.get("active_player", {}).get("summoner_name", "")

    rag_query = state.get("rag_query", "")
    docs: list[str] = []

    # 先查攻略
    if champion and rag_query:
        guide = retriever.search_guide(champion, rag_query, n=2)
        docs.extend([r["document"] for r in (guide or [])])

    # 装备类事件额外查装备库
    if state["event_name"] == "item_purchased":
        items = retriever.search_items(rag_query, n=2)
        docs.extend([r["document"] for r in (items or [])])

    logger.debug("retrieve: %d docs for %s", len(docs), state["event_name"])
    return {**state, "rag_docs": docs}


# ── 记忆注入 ──────────────────────────────────────────

def inject_memory(state: CoachState) -> CoachState:
    """格式化 PlayerMemory 为 LLM 上下文."""
    injector = _injections.get("injector")
    if not injector:
        return state

    # 从 app 全局获取当前 memory
    from memory.models import PlayerMemory

    import app as _app
    memory: PlayerMemory = getattr(_app, "memory", None)
    if memory is None:
        return state

    ctx = injector.format(memory, token_budget=250)
    logger.debug("inject_memory: %d chars", len(ctx))
    return {**state, "memory_context": ctx}


# ── LLM 润色 ──────────────────────────────────────────

def llm_polish(state: CoachState) -> CoachState:
    """调用 DeepSeek/OpenAI LLM 润色教练建议."""
    llm = _injections.get("llm")
    if not llm or not llm._client:
        return {**state, "polished_message": state["skill_message"]}

    from models.state import CoachingTip

    tip = CoachingTip(
        skill=state["skill_name"],
        message=state["skill_message"],
        priority=state["priority"],
    )

    # 拼接 RAG + 记忆上下文
    rag_ctx = None
    parts = []
    if state.get("rag_docs"):
        parts.append("Knowledge:\n" + "\n".join(state["rag_docs"][:2]))
    if state.get("memory_context"):
        parts.append("Player:\n" + state["memory_context"])
    if parts:
        rag_ctx = "\n".join(parts)

    try:
        result = llm.polish(tip, None, rag_context=rag_ctx)
        state["polished_message"] = result.message
        logger.debug("llm_polish: %s", state["polished_message"][:60])
    except Exception:
        logger.exception("llm_polish failed")
        state["polished_message"] = state["skill_message"]

    return state


# ── 验证 / 去重 ──────────────────────────────────────

def validate(state: CoachState) -> CoachState:
    """去重检查，决定是否发布."""
    redis = _injections.get("redis_store")
    skill = state["skill_name"]

    if not skill:
        return {**state, "should_publish": False, "skip_reason": "no_skill"}

    if redis and redis.was_tip_recently_sent(state["session_id"], skill):
        logger.debug("validate: duplicate skill=%s", skill)
        return {**state, "should_publish": False, "skip_reason": "duplicate"}

    return {**state, "should_publish": True, "skip_reason": ""}


# ── 发布 ──────────────────────────────────────────────

def publish(state: CoachState) -> CoachState:
    """标记 tip 输出，由调用方发送 WebSocket."""
    redis = _injections.get("redis_store")
    if redis:
        redis.mark_tip_sent(state["session_id"], state["skill_name"])

    state["tip"] = {
        "skill": state["skill_name"],
        "message": state["polished_message"],
        "priority": state["priority"],
    }
    logger.info("[%s] %s", state["skill_name"], state["polished_message"][:80])
    return state
