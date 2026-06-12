from models.state import CoachEvent, GameState


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    count = event.data.get("item_count", 0)
    return f"New item purchased ({count} items) — check enemy build and adapt."
