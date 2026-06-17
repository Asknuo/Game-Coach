from models.state import CoachEvent, GameState
from knowledge.retriever import get_retriever


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    game_time = event.data.get("game_time", 0)
    minutes = int(game_time // 60)
    base = f"{minutes} min mark — check jungle camps and track enemy jungler."

    retriever = get_retriever()
    if retriever and state:
        champion = state.active_player.summoner_name
        guide_results = retriever.search_guide_by_time(
            champion,
            game_time,
            "jungle route pathing gank strategy what to do",
            n=1,
        )
        if guide_results:
            rag = guide_results[0]["document"]
            short = rag[:80]
            return f"{base} ({short})"

    return base
