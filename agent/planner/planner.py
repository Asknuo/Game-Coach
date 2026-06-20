"""事件 → Skill 路由器。

加载 SKILL.md 的 frontmatter 作为 skill 注册表，
加载 SKILL.md 正文作为 coaching 上下文提供给 LLM。
"""

import logging
import os
from typing import Optional

import yaml

from models.state import CoachEvent, CoachingTip, GameState

logger = logging.getLogger(__name__)

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")


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
        logger.info("Skill loaded: %s — %s", folder_name, fm.get("description", "")[:60])
    return registry


# 全局注册表
SKILL_REGISTRY = build_registry()

# 事件 → skill 反向索引
EVENT_TO_SKILL: dict[str, str] = {}
for skill_name, meta in SKILL_REGISTRY.items():
    for event in meta.get("events", []):
        EVENT_TO_SKILL[event] = skill_name

logger.info("Skills registered: %d skills, %d event mappings", len(SKILL_REGISTRY), len(EVENT_TO_SKILL))


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
    def _build_base_message(event: CoachEvent, state: GameState | None, skill: str, meta: dict) -> str:
        """根据事件构建基础建议文本."""
        data = event.data or {}
        name = event.name

        if name == "low_health":
            hp = state.active_player_health_pct() if state else None
            hp_str = f" ({hp:.0f}% HP)" if hp is not None else ""
            return f"Low HP{hp_str} — check if recall is needed."

        elif name == "dragon_soon":
            sec = data.get("seconds_left", 30)
            return f"Dragon spawning in {sec}s — prepare vision and positioning."

        elif name == "baron_soon":
            sec = data.get("seconds_left", 30)
            return f"Baron spawning in {sec}s — set up vision, do NOT start Baron."

        elif name == "item_purchased":
            return "Item purchased — consider next item based on enemy composition."

        elif name == "item_sold":
            item_id = data.get("item_id", data.get("new_item_id", "unknown"))
            return f"Item sold (ID: {item_id}) — inventory space freed."

        elif name == "item_upgraded":
            old_id = data.get("old_item_id", "unknown")
            new_id = data.get("new_item_id", "unknown")
            return f"Item upgraded (ID: {old_id} → {new_id}) — power spike incoming."

        elif name == "enemy_item_purchased":
            enemy_champ = data.get("enemy_champion", "enemy")
            item_count = data.get("total_items", 0)
            return f"Enemy {enemy_champ} bought item(s) (total: {item_count}) — check their build and counter."

        elif name == "enemy_item_sold":
            enemy_name = data.get("enemy_name", "enemy")
            return f"Enemy {enemy_name} sold item(s) — possible item slot swap or build pivot."

        elif name == "enemy_gold_lead":
            enemy_champ = data.get("enemy_champion", "enemy")
            gap = data.get("gold_gap", 0)
            kills = data.get("enemy_kills", 0)
            return f"Enemy {enemy_champ} has a {gap:.0f}g lead ({kills} kills) — avoid 1v1, play safe and coordinate ganks."

        elif name == "enemy_fed":
            enemy_champ = data.get("enemy_champion", "enemy")
            kills = data.get("kills", 0)
            milestone = data.get("milestone", 0)
            return f"ENEMY FED: {enemy_champ} reached {kills} kills — high shutdown priority, group to shut them down."

        elif name == "gold_spike":
            return f"Gold spike ({data.get('delta', 0)}g) — consider your next purchase."

        elif name == "kill":
            kills = data.get("total_kills", 1)
            return f"Kill secured ({kills} total) — capitalize on the numbers advantage."

        elif name == "laning_check":
            return "Laning phase check — wave management and trading advice."

        elif name == "macro_check":
            return "Macro check — team rotation and objective priority."

        elif name == "teamfight_detected":
            return "Teamfight detected — target priority and positioning."

        elif name == "game_end":
            return "Game ended — generating post-game review."

        return f"[{name}] Coaching advice."


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
