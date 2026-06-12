from models.state import CoachEvent, CoachingTip, GameState

EVENT_SKILL_MAP: dict[str, str] = {
    "dragon_soon": "dragon",
    "baron_soon": "dragon",
    "low_health": "recall",
    "item_purchased": "build",
    "jungle_check": "jungle",
}

PRIORITY: dict[str, int] = {
    "low_health": 3,
    "dragon_soon": 2,
    "baron_soon": 2,
    "item_purchased": 1,
    "jungle_check": 1,
}


class Planner:
    """Select the best skill to handle an incoming event."""

    def plan(self, event: CoachEvent, state: GameState | None) -> CoachingTip | None:
        skill_name = EVENT_SKILL_MAP.get(event.name)
        if not skill_name:
            return None

        from skills import build, dragon, jungle, recall

        registry = {
            "dragon": dragon,
            "recall": recall,
            "build": build,
            "jungle": jungle,
        }

        module = registry.get(skill_name)
        if module is None:
            return None

        message = module.generate(event, state)
        if not message:
            return None

        return CoachingTip(
            message=message,
            skill=skill_name,
            priority=PRIORITY.get(event.name, 1),
        )
