"""数据格式化器 — 将 Data Dragon JSON 转为可供 embedding 的自然语言描述."""

import re


class ItemFormatter:
    """将装备 JSON 转为可供 embedding 的自然语言描述。"""

    @staticmethod
    def format(item: dict) -> str:
        name = item.get("name", "Unknown Item")
        plaintext = item.get("plaintext", "")
        description = item.get("description", "")
        gold = item.get("gold", {})
        cost = gold.get("total", item.get("cost", 0))
        tags = item.get("tags", [])
        stats = item.get("stats", {})
        from_items = item.get("from", [])
        into_items = item.get("into", [])

        parts = [f"{name}:"]

        # 使用 Data Dragon 的 description（含 HTML 标签），做简单清理
        clean_desc = None
        if plaintext:
            parts.append(plaintext + ".")
        if description:
            clean_desc = description
            # 移除 HTML 标签
            clean_desc = re.sub(r"<br\s*/?>", ". ", clean_desc)
            clean_desc = re.sub(r"<[^>]+>", "", clean_desc)
            clean_desc = re.sub(r"\s+", " ", clean_desc).strip()
            if clean_desc and clean_desc != plaintext:
                parts.append(clean_desc)
            elif clean_desc and not plaintext:
                parts.append(clean_desc)

        if stats:
            stat_parts = []
            for k, v in stats.items():
                readable = k.replace("Flat", "").replace("Percent", "%").replace("Mod", "")
                readable_str = "".join(" " + c if c.isupper() else c for c in readable).strip()
                stat_parts.append(f"+{v} {readable_str}")
            parts.append("Stats: " + ", ".join(stat_parts))
        parts.append(f"Cost: {cost}g")
        if tags:
            parts.append(f"Tags: {', '.join(tags)}")
        if from_items:
            parts.append(f"Builds from: {', '.join(str(x) for x in from_items)}")
        if into_items:
            parts.append(f"Builds into: {', '.join(str(x) for x in into_items)}")
        return ". ".join(parts)


class ChampionFormatter:
    """将英雄技能数据转为自然语言描述。"""

    @staticmethod
    def format_ability(champion: str, key: str, ability_name: str, description: str) -> str:
        # 清理 HTML 标签
        clean = description
        clean = re.sub(r"<br\s*/?>", ". ", clean)
        clean = re.sub(r"<[^>]+>", "", clean)
        # 移除 Data Dragon 的缩放标记
        clean = re.sub(r"\{\{[^}]+\}\}", "", clean)
        clean = re.sub(r"@[a-zA-Z.]+@", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()

        label = "Passive" if key == "被动" else f"{key} -"
        return f"[{champion}] {label} {ability_name}: {clean}"
