"""三级记忆数据模型 — DeerFlow 架构风格
- user:   短期/活跃记忆，每帧 state 更新
- history: 中期/长期记忆，对局结束后沉淀
- facts:  结构化长期记忆，可检索
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 第一层: User Context (对局实时状态快照)
# ──────────────────────────────────────────────

class UserContext(BaseModel):
    current_champion: str = ""
    champion_role: str = ""         # top / jungle / mid / bot / support
    current_gold: float = 0
    current_level: int = 0
    kda: dict[str, int] = Field(default_factory=lambda: {"kills": 0, "deaths": 0, "assists": 0})
    game_phase: str = "early"       # early / mid / late
    top_of_mind: list[str] = Field(default_factory=list)
    updated_at: str = ""


# ──────────────────────────────────────────────
# 第二层: History (跨对局行为轨迹)
# ──────────────────────────────────────────────

class RecentGame(BaseModel):
    game_id: str = ""
    champion: str = ""
    result: str = ""                # win / loss
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    cs_at_10: int = 0
    gold_diff_at_15: float = 0
    key_moment: str = ""
    mistake: str = ""
    played_at: str = ""


class History(BaseModel):
    recent_games: list[RecentGame] = Field(default_factory=list)
    champion_pool: dict[str, dict[str, Any]] = Field(default_factory=dict)
    common_mistakes: list[dict[str, Any]] = Field(default_factory=list)
    recent_months: str = ""
    earlier_context: str = ""
    updated_at: str = ""


# ──────────────────────────────────────────────
# 第三层: Facts (结构化长期记忆)
# ──────────────────────────────────────────────

class Fact(BaseModel):
    id: str
    content: str
    category: str                   # preference / knowledge / behavior / goal / matchup
    confidence: float = 0.5
    created_at: str = ""
    source: str = ""                # game_id or "manual"


# ──────────────────────────────────────────────
# 顶层 Memory
# ──────────────────────────────────────────────

class PlayerMemory(BaseModel):
    version: str = "1.0"
    session_id: str = "default"
    user: UserContext = Field(default_factory=UserContext)
    history: History = Field(default_factory=History)
    facts: list[Fact] = Field(default_factory=list)
    last_updated: str = ""


# ──────────────────────────────────────────────
# 工厂方法
# ──────────────────────────────────────────────

def create_empty_memory(session_id: str = "default") -> PlayerMemory:
    return PlayerMemory(
        session_id=session_id,
        last_updated=datetime.utcnow().isoformat(),
    )
