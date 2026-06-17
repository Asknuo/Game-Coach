from models.state import CoachEvent, GameState
from knowledge.retriever import get_retriever


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    count = event.data.get("item_count", 0)
    base = f"New item purchased ({count} items)"

    retriever = get_retriever()
    if retriever and state:
        champion = state.active_player.summoner_name
        guide_results = retriever.search_guide(
            champion,
            "recommended build items core build order next item",
            n=2,
        )
        if guide_results:
            rag_text = guide_results[0]["document"]
            short = rag_text[:60]
            return f"{base}. {short}"

    if retriever:
        item_results = retriever.search_items(
            "recommended next item to build",
            n=1,
        )
        if item_results:
            name = item_results[0]["metadata"].get("name", "")
            if name:
                return f"{base} — consider {name}."

    return f"{base} — check enemy build and adapt."
