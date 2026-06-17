from models.state import CoachEvent, GameState
from knowledge.retriever import get_retriever


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    hp = event.data.get("health_pct")
    if hp is None and state:
        hp = state.active_player_health_pct()
    base = f"Health at {hp:.0f}% — consider recalling to base safely."

    retriever = get_retriever()
    if retriever and state:
        champion = state.active_player.summoner_name
        guide_results = retriever.search_guide(
            champion,
            "when low health recall sustain laning phase recovery safe",
            n=1,
        )
        if guide_results:
            rag = guide_results[0]["document"]
            short = rag[:80]
            return f"{base} ({short})"

    return base
