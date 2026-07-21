from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class Vec2(BaseModel):
    x: float = 0
    y: float = 0


class Item(BaseModel):
    item_id: int = Field(0, alias="itemID")
    slot: int = 0

    model_config = {"populate_by_name": True}


class ActivePlayer(BaseModel):
    summoner_name: str = ""
    champion_name: str = ""           # 英雄名（如 "Ahri"），Live Client API 的
                                      # activePlayer 不含此字段，由 sync_active_player
                                      # 从 all_players 匹配补全；Go 侧 MergeActivePlayer 也会同步
    team: str = ""
    level: int = 0
    current_gold: float = 0
    health: float = 0
    max_health: float = 0
    kills: int = 0                    # 以下三项同样由 sync_active_player 从 all_players 补全
    deaths: int = 0
    assists: int = 0
    position: Vec2 = Field(default_factory=Vec2)
    items: list[Item] = Field(default_factory=list)


class Player(BaseModel):
    summoner_name: str = ""
    team: str = ""
    champion_name: str = ""
    level: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    current_gold: float = 0.0
    creep_score: int = 0
    health: float = 0
    max_health: float = 0
    position: Vec2 = Field(default_factory=Vec2)
    items: list[Item] = Field(default_factory=list)


class GameEvent(BaseModel):
    event_id: int = 0
    event_name: str = ""
    event_time: float = 0
    dragon_type: str = ""


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

    @field_validator("all_players", "events", mode="before")
    @classmethod
    def _none_to_empty_list(cls, v):
        """Coerce null → [], defensive against Go nil-slice serialization."""
        if v is None:
            return []
        return v

    def active_player_health_pct(self) -> float:
        if self.active_player.max_health <= 0:
            return 100.0
        return self.active_player.health / self.active_player.max_health * 100

    def sync_active_player(self) -> None:
        """Populate ActivePlayer items / champion_name / KDA from AllPlayers.

        Live Client API 的 activePlayer 端点不含 champion_name 和 KDA，
        需从 all_players 中按 summoner_name 匹配补全。RAG 检索、
        记忆注入、复盘摘要都依赖这些字段。"""
        for p in self.all_players:
            if p.summoner_name == self.active_player.summoner_name:
                self.active_player.items = p.items
                self.active_player.champion_name = p.champion_name
                self.active_player.kills = p.kills
                self.active_player.deaths = p.deaths
                self.active_player.assists = p.assists
                return


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


# ── 位置工具函数 ──


def get_position_distance(a: dict | Vec2, b: dict | Vec2) -> float:
    """计算两个坐标之间的欧氏距离。适用 dict 和 Vec2 输入."""
    ax = a.get("x", 0) if isinstance(a, dict) else a.x
    ay = a.get("y", 0) if isinstance(a, dict) else a.y
    bx = b.get("x", 0) if isinstance(b, dict) else b.x
    by = b.get("y", 0) if isinstance(b, dict) else b.y
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def is_position_valid(pos: dict | Vec2) -> bool:
    """检查坐标是否有效（非 (0,0) 且非空）."""
    if isinstance(pos, dict):
        x = pos.get("x", 0)
        y = pos.get("y", 0)
    else:
        x, y = pos.x, pos.y
    return x != 0 or y != 0
