from models.state import CoachEvent, GameState


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    seconds_left = event.data.get("seconds_left", 30)
    if event.name == "baron_soon":
        return f"Baron in {int(seconds_left)}s — establish vision and group with team."
    return f"Dragon in {int(seconds_left)}s — ward river and prepare to contest."
