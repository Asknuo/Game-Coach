"""LLM 润色的上下文组装 — 显式分层 + token 预算。

此前 llm_polish 节点把四段上下文裸拼接（`"\\n\\n".join(parts)`），
哪一层该保、哪一层可裁在代码里没有任何表达。这里把四层显式排序并加预算：

按“预算不足时的裁剪顺序”（高优先级在前）：
  1. gotchas    坑点约束    — 系统最高信号，永不裁剪
  2. memory     玩家上下文  — 体量小且决定个性化，永不裁剪
  3. rag        事件相关知识 — 与当前局势强相关
  4. guidelines SKILL.md 指导方针 — 静态、最泛化，预算不足时最先裁

裁剪只在 markdown section（`## ` 标题）边界进行，不会产生半句话。
输出顺序与裁剪顺序无关，仍按“指导方针 → 坑点 → 知识 → 记忆”呈现。
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# CJK 字符 ≈ 1 token，其余 ≈ 4 char/token（与 memory/injector.py 的粗估口径一致）
_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SECTION_SPLIT = re.compile(r"\n(?=## )")

# 默认预算：当前 7 个 skill 的注入总量在其之下，主要用于兜住未来膨胀，
# 需要更激进地压缩提示词时通过 LLM_CONTEXT_BUDGET_TOKENS 调低。
DEFAULT_BUDGET_TOKENS = 6000

HEADERS = {
    "guidelines": "=== Coaching Guidelines ===",
    "gotchas": "=== CRITICAL Gotchas (do NOT give wrong advice) ===",
    "rag": "=== Game Knowledge ===",
    "memory": "=== Player Context ===",
}
# 展示顺序（与裁剪决策无关）
DISPLAY_ORDER = ("guidelines", "gotchas", "rag", "memory")
# 预算不足时按此顺序裁剪（列表末尾最先被裁）；gotchas / memory 不在其中
TRIM_ORDER = ("guidelines", "rag")


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 字符 ≈ 1 token，其余 ≈ 4 char/token."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + (len(text) - cjk) // 4


def _budget_tokens() -> int:
    raw = os.getenv("LLM_CONTEXT_BUDGET_TOKENS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_TOKENS
    return value if value > 0 else DEFAULT_BUDGET_TOKENS


def _truncate_sections(text: str, max_tokens: int) -> str:
    """按 `## ` 分节，从尾部整节丢弃直到 ≤ max_tokens；首节自身超标则整段丢弃."""
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    sections = _SECTION_SPLIT.split(text)
    kept: list[str] = []
    for section in sections:
        candidate = "\n".join([*kept, section])
        if estimate_tokens(candidate) > max_tokens:
            break
        kept.append(section)
    return "\n".join(kept).strip()


def build_polish_context(
    *,
    skill_context: str = "",
    skill_gotchas: str = "",
    rag_docs: list[str] | None = None,
    memory_context: str = "",
    budget_tokens: int | None = None,
) -> str | None:
    """组装四层上下文，预算不足时按优先级裁剪。四层全空时返回 None."""
    sources = {
        "guidelines": (skill_context or "").strip(),
        "gotchas": (skill_gotchas or "").strip(),
        "rag": "\n".join((rag_docs or [])[:2]).strip(),
        "memory": (memory_context or "").strip(),
    }
    if not any(sources.values()):
        return None

    budget = _budget_tokens() if budget_tokens is None else budget_tokens

    def total() -> int:
        return sum(estimate_tokens(v) for v in sources.values())

    for key in TRIM_ORDER:
        if total() <= budget:
            break
        over = total() - budget
        sources[key] = _truncate_sections(
            sources[key], max(0, estimate_tokens(sources[key]) - over)
        )

    final = total()
    if final > budget:
        # gotchas / memory 永不裁剪，可能仅剩它们仍超预算——只告警不丢内容
        logger.warning("context over budget after trim (%d > %d tokens)", final, budget)
    logger.debug(
        "context tokens=%d budget=%d breakdown=%s",
        final, budget,
        {k: estimate_tokens(v) for k, v in sources.items()},
    )

    parts = [f"{HEADERS[key]}\n{sources[key]}" for key in DISPLAY_ORDER if sources[key]]
    return "\n\n".join(parts)