"""生成阶段节点 — inject_memory / llm_polish."""

import logging

from graph.state import CoachState

logger = logging.getLogger(__name__)


class GenerationMixin:
    """记忆注入与 LLM 润色节点（依赖 injector / memory / llm）."""

    def inject_memory(self, state: CoachState) -> CoachState:
        """格式化 PlayerMemory 为 LLM 上下文."""
        injector = self.deps.injector
        if not injector:
            return state

        memory = self.deps.memory
        if memory is None:
            return state

        ctx = injector.format(memory, token_budget=200)
        logger.debug("inject_memory: %d chars", len(ctx))
        return {**state, "memory_context": ctx}

    async def llm_polish(self, state: CoachState) -> CoachState:
        """调用 LLM 润色教练建议，注入 SKILL.md 上下文 + 坑点清单."""
        llm = self.deps.llm
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
            # 异步链路：不阻塞 FastAPI 事件循环
            result = await llm.apolish(tip, None, rag_context=rag_ctx)
            state["polished_message"] = result.message
            logger.debug("llm_polish: %s", state["polished_message"][:80])
        except Exception:
            logger.exception("llm_polish failed")
            state["polished_message"] = state["skill_message"]

        return state
