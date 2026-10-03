"""生成阶段节点 — inject_memory / llm_polish."""

import logging
import time

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
        if not llm or not llm._get_async_client():
            return {**state, "polished_message": state["skill_message"]}

        from models.state import CoachingTip, GameState

        tip = CoachingTip(
            skill=state["skill_name"],
            message=state["skill_message"],
            priority=state["priority"],
        )

        gs = state.get("game_state")
        try:
            snapshot = GameState.model_validate(gs) if gs else None
        except Exception:
            snapshot = None

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

        emitter = self.deps.on_polish_delta
        try:
            if emitter:
                # 流式：增量实时推送 overlay（0.1s 节流，避免逐 token 洪泛）。
                # 最终完整文本再推一次，随后 session 层的权威 tip 消息落锤。
                pieces: list[str] = []
                last_emit = 0.0
                async for delta in llm.apolish_stream(tip, snapshot, rag_context=rag_ctx):
                    pieces.append(delta)
                    now = time.monotonic()
                    if now - last_emit >= 0.1:
                        last_emit = now
                        try:
                            await emitter(tip, "".join(pieces))
                        except Exception:
                            logger.exception("polish delta emit failed")
                if pieces:
                    try:
                        await emitter(tip, "".join(pieces))
                    except Exception:
                        logger.exception("polish final emit failed")
                text = "".join(pieces).strip()
                state["polished_message"] = text if text else state["skill_message"]
            else:
                # 非流式（测试 / 未注入 emitter 的场景）：行为与原实现一致
                result = await llm.apolish(tip, snapshot, rag_context=rag_ctx)
                state["polished_message"] = result.message
            logger.debug("llm_polish: %s", state["polished_message"][:80])
        except Exception:
            logger.exception("llm_polish failed")
            state["polished_message"] = state["skill_message"]

        return state
