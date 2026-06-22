"""事件 → Skill 路由器。

加载 SKILL.md 的 frontmatter 作为 skill 注册表，
加载 SKILL.md 正文作为 coaching 上下文提供给 LLM。
"""

import logging
import os
from collections.abc import Callable

import yaml

from models.state import CoachEvent, CoachingTip, GameState

logger = logging.getLogger(__name__)

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")

EventMessageBuilder = Callable[[CoachEvent, GameState | None, dict], str]


def _base_low_health(_event: CoachEvent, state: GameState | None, _data: dict) -> str:
    hp = state.active_player_health_pct() if state else None
    hp_str = f" ({hp:.0f}% HP)" if hp is not None else ""
    return f"Low HP{hp_str} — check if recall is needed."


def _base_dragon_soon(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    sec = data.get("seconds_left", 30)
    return f"Dragon spawning in {sec}s — prepare vision and positioning."


def _base_baron_soon(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    sec = data.get("seconds_left", 30)
    return f"Baron spawning in {sec}s — set up vision, do NOT start Baron."


def _base_item_purchased(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "Item purchased — consider next item based on enemy composition."


def _base_item_sold(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    item_id = data.get("item_id", data.get("new_item_id", "unknown"))
    return f"Item sold (ID: {item_id}) — inventory space freed."


def _base_item_upgraded(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    old_id = data.get("old_item_id", "unknown")
    new_id = data.get("new_item_id", "unknown")
    return f"Item upgraded (ID: {old_id} → {new_id}) — power spike incoming."


def _base_enemy_item_purchased(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_champ = data.get("enemy_champion", "enemy")
    item_count = data.get("total_items", 0)
    return (
        f"Enemy {enemy_champ} bought item(s) (total: {item_count}) "
        f"— check their build and counter."
    )


def _base_enemy_item_sold(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_name = data.get("enemy_name", "enemy")
    return f"Enemy {enemy_name} sold item(s) — possible item slot swap or build pivot."


def _base_enemy_gold_lead(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_champ = data.get("enemy_champion", "enemy")
    gap = data.get("gold_gap", 0)
    kills = data.get("enemy_kills", 0)
    return (
        f"Enemy {enemy_champ} has a {gap:.0f}g lead ({kills} kills) "
        f"— avoid 1v1, play safe and coordinate ganks."
    )


def _base_enemy_fed(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_champ = data.get("enemy_champion", "enemy")
    kills = data.get("kills", 0)
    return (
        f"ENEMY FED: {enemy_champ} reached {kills} kills "
        f"— high shutdown priority, group to shut them down."
    )


def _base_gold_spike(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    return f"Gold spike ({data.get('delta', 0)}g) — consider your next purchase."


def _base_kill(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    kills = data.get("total_kills", 1)
    return f"Kill secured ({kills} total) — capitalize on the numbers advantage."


def _base_laning_check(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "Laning phase check — wave management and trading advice."


def _base_macro_check(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "Macro check — team rotation and objective priority."


def _base_teamfight_detected(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "Teamfight detected — target priority and positioning."


def _base_game_end(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "Game ended — generating post-game review."


EVENT_BASE_MESSAGES: dict[str, EventMessageBuilder] = {
    "low_health": _base_low_health,
    "dragon_soon": _base_dragon_soon,
    "baron_soon": _base_baron_soon,
    "item_purchased": _base_item_purchased,
    "item_sold": _base_item_sold,
    "item_upgraded": _base_item_upgraded,
    "enemy_item_purchased": _base_enemy_item_purchased,
    "enemy_item_sold": _base_enemy_item_sold,
    "enemy_gold_lead": _base_enemy_gold_lead,
    "enemy_fed": _base_enemy_fed,
    "gold_spike": _base_gold_spike,
    "kill": _base_kill,
    "laning_check": _base_laning_check,
    "macro_check": _base_macro_check,
    "teamfight_detected": _base_teamfight_detected,
    "game_end": _base_game_end,
}


def _parse_frontmatter(content: str) -> dict:
    """解析 YAML frontmatter（--- 之间的部分）."""
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(content[3:end]) or {}
    except yaml.YAMLError:
        return {}


def _load_skill_md(skill_name: str) -> tuple[dict, str]:
    """加载某个 skill 的 SKILL.md，返回 (frontmatter, body)."""
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if not os.path.isfile(path):
        logger.warning("SKILL.md not found: %s", path)
        return {}, ""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    fm = _parse_frontmatter(content)
    # 去掉 frontmatter 后的正文
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            body = content[end + 3:].strip()
        else:
            body = content
    else:
        body = content
    return fm, body


# ── 启动时构建注册表 ──
def build_registry() -> dict[str, dict]:
    """遍历 skills/ 目录，从每个子目录的 SKILL.md 构建注册表."""
    registry = {}
    for folder_name in sorted(os.listdir(SKILLS_DIR)):
        folder_path = os.path.join(SKILLS_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue
        md_path = os.path.join(folder_path, "SKILL.md")
        if not os.path.isfile(md_path):
            continue
        fm, body = _load_skill_md(folder_name)
        fm["_body"] = body
        fm["_folder"] = folder_name
        registry[folder_name] = fm
        logger.debug("Skill loaded: %s — %s", folder_name, fm.get("description", "")[:60])
    return registry


# 全局注册表
SKILL_REGISTRY = build_registry()

# 事件 → skill 反向索引
EVENT_TO_SKILL: dict[str, str] = {}
for skill_name, meta in SKILL_REGISTRY.items():
    for event in meta.get("events", []):
        EVENT_TO_SKILL[event] = skill_name

logger.debug("Skills registered: %d skills, %d event mappings", len(SKILL_REGISTRY), len(EVENT_TO_SKILL))


class Planner:
    """根据事件名查找对应的 Skill，加载 SKILL.md 作为上下文."""

    def plan(self, event: CoachEvent, state: GameState | None) -> CoachingTip | None:
        skill_name = EVENT_TO_SKILL.get(event.name)
        if not skill_name:
            logger.debug("No skill for event: %s", event.name)
            return None

        meta = SKILL_REGISTRY.get(skill_name)
        if not meta:
            return None

        # 生成基础消息（事件 + 关键数据摘要）
        message = self._build_base_message(event, state, skill_name, meta)

        if not message:
            return None

        return CoachingTip(
            message=message,
            skill=skill_name,
            priority=meta.get("priority", 1),
        )

    @staticmethod
    def _build_base_message(
        event: CoachEvent,
        state: GameState | None,
        _skill: str,
        _meta: dict,
    ) -> str:
        """根据事件构建基础建议文本."""
        builder = EVENT_BASE_MESSAGES.get(event.name)
        if builder:
            return builder(event, state, event.data or {})
        return f"[{event.name}] Coaching advice."


def get_skill_context(skill_name: str) -> str:
    """获取某个 skill 的 SKILL.md 正文，用于注入 LLM 上下文."""
    meta = SKILL_REGISTRY.get(skill_name, {})
    return meta.get("_body", "")


def get_skill_gotchas(skill_name: str) -> str:
    """获取某个 skill 的 gotchas.md 内容."""
    path = os.path.join(SKILLS_DIR, skill_name, "gotchas.md")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()
