"""Skill: 定时英雄策略提示 — 根据当前英雄 + 游戏时间检索攻略并推送."""

from models.state import CoachEvent, GameState
from knowledge.retriever import get_retriever


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    if not state:
        return None
    champion = event.data.get("champion", state.active_player.summoner_name)
    game_time = event.data.get("game_time", 0)

    retriever = get_retriever()
    if not retriever:
        return None

    results = retriever.search_guide_by_time(
        champion,
        game_time,
        query="what should I do now key strategy tips priority decisions",
        n=2,
    )
    if not results:
        return None

    # 拼接前两个结果作为上下文
    rag_docs = [r["document"] for r in results]
    combined = " | ".join(rag_docs)[:200]
    return f"Tip for {champion}: {combined}"
