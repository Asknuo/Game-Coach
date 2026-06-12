from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class Vec2(BaseModel):
    x: float = 0
    y: float = 0


class Item(BaseModel):
    item_id: int = Field(0, alias="itemID")
    slot: int = 0

    model_config = {"populate_by_name": True}


class ActivePlayer(BaseModel):
    summoner_name: str = ""
    level: int = 0
    current_gold: float = 0
    health: float = 0
    max_health: float = 0
    position: Vec2 = Field(default_factory=Vec2)


class Player(BaseModel):
    summoner_name: str = ""
    team: str = ""
    level: int = 0
    health: float = 0
    max_health: float = 0
    position: Vec2 = Field(default_factory=Vec2)
    items: list[Item] = Field(default_factory=list)


class GameEvent(BaseModel):
    event_id: int = 0
    event_name: str = ""
    event_time: float = 0


class DragonInfo(BaseModel):
    type: str = "unknown"
    spawn_time: float = 0
    seconds_left: float = 0


class BaronInfo(BaseModel):
    spawn_time: float = 0
    seconds_left: float = 0


class GameState(BaseModel):
    game_time: float = 0
    active_player: ActivePlayer = Field(default_factory=ActivePlayer)
    all_players: list[Player] = Field(default_factory=list)
    events: list[GameEvent] = Field(default_factory=list)
    dragon_timer: Optional[DragonInfo] = None
    baron_timer: Optional[BaronInfo] = None
    collected_at: Optional[datetime] = None

    def active_player_health_pct(self) -> float:
        if self.active_player.max_health <= 0:
            return 100.0
        return self.active_player.health / self.active_player.max_health * 100


class CoachEvent(BaseModel):
    name: str
    data: dict[str, Any] = Field(default_factory=dict)


class CoachingTip(BaseModel):
    message: str
    skill: str
    priority: int = 1


class WSMessage(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
