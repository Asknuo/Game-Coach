"""生成阶段节点 — inject_memory / llm_polish."""

import itertools
import logging
import time

from graph.state import CoachState
from prompt.context_builder import build_polish_context

logger = logging.getLogger(__name__)

# 进程内自增：tip_id = session:event:seq，用于关联流式增量、权威 tip 与撤回消息
_tip_seq = itertools.count(1)


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
        if not llm or not llm.is_available():
            return {**state, "polished_message": state["skill_message"]}

        from models.state import CoachingTip, GameState

        # 每条流水线生成唯一 tip_id：validate/publish 拒发时据此撤回流式卡片
        tip_id = f"{state.get('session_id', 'default')}:{state.get('event_name', '?')}:{next(_tip_seq)}"

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
        # 分层与 token 预算统一由 build_polish_context 处理（gotchas/memory 永不裁剪）
        rag_ctx = build_polish_context(
            skill_context=state.get("skill_context", ""),
            skill_gotchas=state.get("skill_gotchas", ""),
            rag_docs=state.get("rag_docs"),
            memory_context=state.get("memory_context", ""),
        )

        emitter = self.deps.on_polish_delta
        try:
            if emitter:
                # 流式：增量实时推送 overlay（0.1s 节流，避免逐 token 洪泛）。
                # 最终完整文本再推一次，随后 session 层的权威 tip 消息落锤。
                # 流中断时保留已产出部分——overlay 上已显示的内容必须与最终
                # 落锤文本一致，不能回退到观众没见过的草稿。
                pieces: list[str] = []
                last_emit = 0.0
                try:
                    async for delta in llm.apolish_stream(tip, snapshot, rag_context=rag_ctx):
                        pieces.append(delta)
                        now = time.monotonic()
                        if now - last_emit >= 0.1:
                            last_emit = now
                            try:
                                await emitter(tip, "".join(pieces), tip_id)
                            except Exception:
                                logger.exception("polish delta emit failed")
                except Exception:
                    # 流中断（网络/API 故障）：保留部分产出，交由最终 emit 对齐
                    logger.exception("polish stream interrupted — keeping partial text")
                if pieces:
                    try:
                        await emitter(tip, "".join(pieces), tip_id)
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

        state["tip_id"] = tip_id
        return state
