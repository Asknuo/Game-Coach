from models.state import CoachEvent, GameState


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    game_time = event.data.get("game_time", 0)
    minutes = int(game_time // 60)
    return f"{minutes} min mark — check jungle camps and track enemy jungler."
