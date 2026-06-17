"""记忆注入层 — 格式化记忆并注入 System Prompt，Token 预算感知."""

from memory.models import PlayerMemory


class MemoryInjector:
    """将 PlayerMemory 格式化为 LLM 可消费的上下文文本.

    Rule:
    - user 层 (对局实时) 优先级最高
    - facts 中高置信度 (>=0.7) 优先
    - history 最近对局作为补充
    - 总量受 token_budget 约束 (粗估 1 token ≈ 4 char)
    """

    def format(self, memory: PlayerMemory, token_budget: int = 300) -> str:
        parts: list[str] = []
        u = memory.user

        # 1. 对局实时状态
        if u.current_champion:
            parts.append(
                f"Current game: {u.current_champion} ({u.champion_role or 'unknown role'})"
                f" | Phase: {u.game_phase}"
                f" | KDA: {u.kda.get('kills', 0)}/{u.kda.get('deaths', 0)}/{u.kda.get('assists', 0)}"
                f" | Gold: {int(u.current_gold)} | Lv: {u.current_level}"
            )
        if u.top_of_mind:
            parts.append(f"Focus: {'; '.join(u.top_of_mind[-4:])}")

        # 2. Facts (高置信度)
        high_conf = [f for f in memory.facts if f.confidence >= 0.7]
        if high_conf:
            # 按类别分组展示
            by_cat: dict[str, list[str]] = {}
            for f in high_conf[:8]:
                by_cat.setdefault(f.category, []).append(f.content)
            for cat, items in by_cat.items():
                parts.append(f"[{cat}] {' | '.join(items[:3])}")

        # 3. 最近对局
        for i, g in enumerate(memory.history.recent_games[:3]):
            parts.append(
                f"Game -{i+1}: {g.champion} {g.result} "
                f"({g.kills}/{g.deaths}/{g.assists})"
                + (f" — {g.key_moment}" if g.key_moment else "")
            )

        # 4. 常见错误
        for m in memory.history.common_mistakes[:2]:
            parts.append(f"Mistake pattern: {m.get('type', '')} [{m.get('frequency', '')}]")

        # 5. Token 预算截断
        result = "\n".join(parts)
        if len(result) > token_budget * 4:
            # 优先保留 user + facts，裁剪 history
            user_facts = "\n".join(parts[: len(parts) - 3])  # 留最后 3 行 history
            if len(user_facts) > token_budget * 4:
                result = user_facts[: token_budget * 4]
            else:
                result = user_facts

        return result

    def format_empty(self) -> str:
        return "No player history yet."
