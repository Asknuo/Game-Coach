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
    hp_str = f"（{hp:.0f}% HP）" if hp is not None else ""
    return f"血量过低{hp_str}——检查是否需要回城。"


def _base_dragon_soon(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    sec = data.get("seconds_left", 30)
    return f"小龙 {sec:.0f} 秒后刷新——提前布置视野和站位。"


def _base_baron_soon(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    sec = data.get("seconds_left", 30)
    return f"大龙 {sec:.0f} 秒后刷新——布置视野，不要贸然开龙。"


def _base_item_purchased(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "已购买装备——根据敌方阵容考虑下一件出装。"


def _base_item_sold(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    item_id = data.get("item_id", data.get("new_item_id", "unknown"))
    return f"装备已出售（ID: {item_id}）——腾出了装备栏位。"


def _base_item_upgraded(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    old_id = data.get("old_item_id", "unknown")
    new_id = data.get("new_item_id", "unknown")
    return f"装备已升级（ID: {old_id} → {new_id}）——强势期到来。"


def _base_enemy_item_purchased(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_champ = data.get("enemy_champion", "敌方")
    item_count = data.get("total_items", 0)
    return (
        f"敌方 {enemy_champ} 购买了装备（共 {item_count} 件）"
        f"——查看其出装并针对性应对。"
    )


def _base_enemy_item_sold(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_name = data.get("enemy_name", "敌方")
    return f"敌方 {enemy_name} 出售了装备——可能在调整出装路线。"


def _base_enemy_gold_lead(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_champ = data.get("enemy_champion", "敌方")
    gap = data.get("gold_gap", 0)
    kills = data.get("enemy_kills", 0)
    return (
        f"敌方 {enemy_champ} 领先 {gap:.0f} 经济（{kills} 击杀）"
        f"——避免单挑，稳住并呼叫打野。"
    )


def _base_enemy_fed(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    enemy_champ = data.get("enemy_champion", "敌方")
    kills = data.get("kills", 0)
    return (
        f"敌方已起飞：{enemy_champ} 已 {kills} 杀"
        f"——优先集火终结，抱团围剿。"
    )


def _base_gold_spike(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    return f"经济突增（{data.get('delta', 0):.0f} 金）——考虑下一次购买。"


def _base_kill(_event: CoachEvent, _state: GameState | None, data: dict) -> str:
    kills = data.get("total_kills", 1)
    return f"击杀到手（总 {kills} 杀）——利用人数优势扩大战果。"


def _base_laning_check(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "对线期检查——兵线管理和换血时机建议。"


def _base_macro_check(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "宏观检查——团队轮转和目标优先级。"


def _base_teamfight_detected(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "检测到团战——目标优先级和站位。"


def _base_game_end(_event: CoachEvent, _state: GameState | None, _data: dict) -> str:
    return "对局结束——生成赛后复盘。"


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
