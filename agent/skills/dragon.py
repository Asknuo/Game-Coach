from models.state import CoachEvent, GameState
from knowledge.retriever import get_retriever


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    seconds_left = event.data.get("seconds_left", 30)
    objective = "Baron" if event.name == "baron_soon" else "Dragon"

    if event.name == "baron_soon":
        base = f"Baron in {int(seconds_left)}s — establish vision and group with team."
    else:
        base = f"Dragon in {int(seconds_left)}s — ward river and prepare to contest."

    retriever = get_retriever()
    if retriever and state:
        champion = state.active_player.summoner_name
        guide_results = retriever.search_guide(
            champion,
            f"{objective} fight positioning objective control strategy",
            n=1,
        )
        if guide_results:
            rag = guide_results[0]["document"]
            extra = _extract_relevant(rag, objective.lower())
            if extra:
                return f"{base} ({extra})"

    return base


def _extract_relevant(doc: str, keyword: str) -> str:
    import re

    sentences = re.split(r"[.;]", doc)
    for s in sentences:
        if keyword in s.lower():
            return s.strip()[:100]
    return doc.split(".")[0].strip()[:100] if doc else ""
