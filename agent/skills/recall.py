from models.state import CoachEvent, GameState


def generate(event: CoachEvent, state: GameState | None) -> str | None:
    hp = event.data.get("health_pct")
    if hp is None and state:
        hp = state.active_player_health_pct()
    return f"Health at {hp:.0f}% — consider recalling to base safely."
