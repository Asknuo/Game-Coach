"""一次性数据摄入脚本。运行方式: python -m knowledge.ingest

先运行 data_fetcher 下载最新数据，然后嵌入所有数据到 ChromaDB。
"""

import json
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

from knowledge.chroma_store import ChromaStore
from knowledge.embedder import Embedder

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class Ingestor:
    """将所有 LOL 游戏数据嵌入 ChromaDB。"""

    def __init__(self):
        self.store = ChromaStore()
        self.embedder = Embedder()

    def ingest_all(self):
        if not self.store.available:
            logger.warning("ChromaDB not available, skipping ingest")
            return
        if not self.embedder.available:
            logger.warning("Embedder not available, skipping ingest")
            return

        self.ingest_items()
        self.ingest_champions()
        self.ingest_guides()
        self.ingest_auto_guides()
        self.ingest_runes()
        self.ingest_summoner_spells()
        self.ingest_game_info()
        self.store.mark_ingested()
        logger.info("Knowledge base ingestion complete — freshness stamp updated")

    # ---------- 装备 ----------

    def ingest_items(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        items_path = os.path.join(data_dir, "items.json")
        if not os.path.exists(items_path):
            logger.warning("items.json not found at %s, run data_fetcher first", items_path)
            return

        with open(items_path, "r", encoding="utf-8") as f:
            raw_items = json.load(f)

        if not raw_items:
            return

        # 兼容两种格式：dict（新，key=itemID）或 list（旧）
        if isinstance(raw_items, dict):
            items = list(raw_items.values())
        else:
            items = raw_items

        # 过滤掉名称缺失的无效条目
        items = [i for i in items if i.get("name")]

        self._rebuild_collection("lol_items", "_items")

        batch_size = 50
        counter = 0
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            docs = []
            ids = []
            metas = []
            for item in batch:
                item_id = item.get("id") or f"unknown_{counter}"
                doc = ItemFormatter.format(item)
                docs.append(doc)
                ids.append(f"item_{item_id}")
                metas.append({
                    "name": item.get("name", ""),
                    "cost": item.get("gold", {}).get("total", item.get("cost", 0)),
                    "tags": ",".join(item.get("tags", [])),
                    "type": "item",
                })
                counter += 1

            embeddings = self.embedder.embed(docs)
            if embeddings is None:
                logger.error("embedding failed at item batch %d", i)
                continue

            self.store._items.add(
                ids=ids,
                embeddings=embeddings,
                documents=docs,
                metadatas=metas,
            )

        logger.info("ingested %d items", self.store._items.count())

    # ---------- 英雄技能 ----------

    def ingest_champions(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        champions_path = os.path.join(data_dir, "champions.json")
        if not os.path.exists(champions_path):
            logger.warning("champions.json not found at %s, run data_fetcher first", champions_path)
            return

        with open(champions_path, "r", encoding="utf-8") as f:
            champions = json.load(f)

        if not champions:
            return

        self._rebuild_collection("lol_champions", "_champions")

        all_docs = []
        all_ids = []
        all_metas = []

        for champ in champions:
            champ_name = champ.get("name", "Unknown")
            champ_key = champ.get("id", champ_name)

            # 英雄基本信息
            title = champ.get("title", "")
            tags = champ.get("tags", [])
            partype = champ.get("partype", "")
            info = champ.get("info", {})
            stats = champ.get("stats", {})

            overview_parts = [
                f"{champ_name}, {title}.",
                f"Roles: {', '.join(tags)}.",
            ]
            if info:
                difficulty = info.get("difficulty", 0)
                overview_parts.append(
                    f"Difficulty: {difficulty}/10. "
                    f"Damage: {info.get('attack', 0)}/10, "
                    f"Defense: {info.get('defense', 0)}/10, "
                    f"Magic: {info.get('magic', 0)}/10, "
                    f"Utility: {info.get('utility', 0)}/10."
                )
            overview_parts.append(f"Resource: {partype}.")

            # 基础属性
            base_stats_parts = []
            stat_map = {
                "hp": "Base HP", "hpperlevel": "HP/lvl", "mp": "Base Mana", "mpperlevel": "Mana/lvl",
                "movespeed": "Move Speed", "armor": "Base Armor", "armorperlevel": "Armor/lvl",
                "spellblock": "Base MR", "spellblockperlevel": "MR/lvl",
                "attackdamage": "Base AD", "attackdamageperlevel": "AD/lvl",
                "attackspeed": "Attack Speed Ratio", "attackspeedperlevel": "AS/lvl",
                "attackrange": "Attack Range", "hpregen": "HP Regen", "hpregenperlevel": "HP Regen/lvl",
                "mpregen": "Mana Regen", "mpregenperlevel": "Mana Regen/lvl",
            }
            for key, label in stat_map.items():
                if key in stats:
                    base_stats_parts.append(f"{label}: {stats[key]}")
            overview_parts.append("Base Stats: " + ", ".join(base_stats_parts))

            overview_doc = ". ".join(overview_parts)

            # 存储英雄概览
            all_docs.append(overview_doc)
            all_ids.append(f"champion_{champ_key}_overview")
            all_metas.append({
                "champion": champ_name,
                "champion_key": champ_key,
                "name": champ_name,
                "section": "overview",
                "type": "champion",
            })

            # 技能 (Passive + QWER)
            spells = champ.get("spells", [])
            # Passive 单独处理
            passive = champ.get("passive", {})
            if passive:
                passive_doc = ChampionFormatter.format_ability(
                    champ_name, "被动", passive.get("name", ""), passive.get("description", "")
                )
                all_docs.append(passive_doc)
                all_ids.append(f"champion_{champ_key}_passive")
                all_metas.append({
                    "champion": champ_name,
                    "champion_key": champ_key,
                    "name": champ_name,
                    "section": "passive",
                    "ability": passive.get("name", ""),
                    "type": "champion",
                })

            spell_keys = ["Q", "W", "E", "R"]
            for idx, spell in enumerate(spells):
                key = spell_keys[idx] if idx < len(spell_keys) else f"Ability{idx+1}"
                spell_doc = ChampionFormatter.format_ability(
                    champ_name, key, spell.get("name", ""), spell.get("description", "")
                )
                all_docs.append(spell_doc)
                all_ids.append(f"champion_{champ_key}_{key.lower()}")
                all_metas.append({
                    "champion": champ_name,
                    "champion_key": champ_key,
                    "name": champ_name,
                    "section": key,
                    "ability": spell.get("name", ""),
                    "type": "champion",
                })

        # 批量嵌入
        batch_size = 50
        for i in range(0, len(all_docs), batch_size):
            batch_docs = all_docs[i : i + batch_size]
            batch_ids = all_ids[i : i + batch_size]
            batch_metas = all_metas[i : i + batch_size]

            embeddings = self.embedder.embed(batch_docs)
            if embeddings is None:
                logger.error("embedding failed at champion batch %d", i)
                continue

            self.store._champions.add(
                ids=batch_ids,
                embeddings=embeddings,
                documents=batch_docs,
                metadatas=batch_metas,
            )

        logger.info("ingested %d champion entries", self.store._champions.count())

    # ---------- 英雄攻略 ----------

    def ingest_guides(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data", "champions")
        if not os.path.exists(data_dir):
            logger.warning("champions dir not found at %s", data_dir)
            return

        self._rebuild_collection("lol_champion_guides", "_guides")

        for filename in os.listdir(data_dir):
            if not filename.endswith(".md"):
                continue
            champion = filename[:-3]
            filepath = os.path.join(data_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            sections = self._split_sections(content)
            docs = []
            ids = []
            metas = []
            for sec in sections:
                doc = f"[{champion}] {sec['heading']}: {sec['body']}"
                docs.append(doc)
                ids.append(f"guide_{champion}_{sec['slug']}")
                metas.append({
                    "champion": champion,
                    "phase": sec.get("phase", "meta"),
                    "heading": sec["heading"],
                    "type": "guide",
                })

            if not docs:
                continue

            embeddings = self.embedder.embed(docs)
            if embeddings is None:
                logger.error("embedding failed for champion %s", champion)
                continue

            self.store._guides.add(
                ids=ids,
                embeddings=embeddings,
                documents=docs,
                metadatas=metas,
            )

        logger.info("ingested %d manual guide chunks", self.store._guides.count())

    # ---------- 自动生成全英雄攻略 ----------

    def ingest_auto_guides(self):
        """基于 Data Dragon 数据 + 角色模板，为全部 172 个英雄自动生成攻略。"""
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        champions_path = os.path.join(data_dir, "champions.json")
        if not os.path.exists(champions_path):
            logger.warning("champions.json not found, skip auto guides")
            return

        with open(champions_path, "r", encoding="utf-8") as f:
            champions = json.load(f)

        if not champions:
            return

        # 先获取已存在的 guide ID 集合（手动 markdown 文件已摄入的），避免覆盖
        existing_ids: set[str] = set()
        if self.store.guides is not None:
            try:
                existing = self.store.guides.get(limit=10000)
                existing_ids = set(existing.get("ids", []))
            except Exception:
                pass

        generator = GuideGenerator()
        all_docs = []
        all_ids = []
        all_metas = []

        for champ in champions:
            name = champ.get("name", "Unknown")
            sections = generator.generate(champ)
            for sec in sections:
                doc_id = f"guide_auto_{name}_{sec['slug']}"
                if doc_id in existing_ids:
                    continue
                all_docs.append(f"[{name}] {sec['heading']}: {sec['body']}")
                all_ids.append(doc_id)
                all_metas.append({
                    "champion": name,
                    "phase": sec.get("phase", "meta"),
                    "heading": sec["heading"],
                    "type": "guide_auto",
                })

        if not all_docs:
            logger.info("no new auto guides to ingest")
            return

        batch_size = 50
        for i in range(0, len(all_docs), batch_size):
            batch_docs = all_docs[i : i + batch_size]
            batch_ids = all_ids[i : i + batch_size]
            batch_metas = all_metas[i : i + batch_size]

            embeddings = self.embedder.embed(batch_docs)
            if embeddings is None:
                logger.error("embedding failed at auto guide batch %d", i)
                continue

            self.store._guides.add(
                ids=batch_ids,
                embeddings=embeddings,
                documents=batch_docs,
                metadatas=batch_metas,
            )

        logger.info("ingested %d auto guide chunks (total guides: %d)",
                     len(all_docs), self.store._guides.count())

    # ---------- 符文 ----------

    def ingest_runes(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        runes_path = os.path.join(data_dir, "runes.json")
        if not os.path.exists(runes_path):
            logger.warning("runes.json not found at %s, run data_fetcher first", runes_path)
            return

        with open(runes_path, "r", encoding="utf-8") as f:
            rune_paths = json.load(f)

        if not rune_paths:
            return

        self._rebuild_collection("lol_runes", "_runes")

        all_docs = []
        all_ids = []
        all_metas = []

        for path in rune_paths:
            path_name = path.get("name", "")
            path_key = path.get("key", "")

            # 符文页概述
            path_doc = f"Rune Path: {path_name}. {path.get('icon', '')}"
            all_docs.append(path_doc)
            all_ids.append(f"rune_path_{path_key}")
            all_metas.append({
                "path": path_name,
                "name": path_name,
                "type": "rune_path",
            })

            # 基石符文 & 普通符文
            for slot in path.get("slots", []):
                for rune in slot.get("runes", []):
                    rune_name = rune.get("name", "")
                    rune_key = rune.get("key", "")
                    short = rune.get("shortDesc", "")
                    long_desc = rune.get("longDesc", "")

                    rune_doc = f"[{path_name}] {rune_name} (Keystone: {slot.get('type','')=='keystone'}): {short}. {long_desc}"
                    all_docs.append(rune_doc)
                    all_ids.append(f"rune_{rune_key}")
                    all_metas.append({
                        "path": path_name,
                        "name": rune_name,
                        "type": "rune",
                    })

        batch_size = 50
        for i in range(0, len(all_docs), batch_size):
            batch_docs = all_docs[i : i + batch_size]
            batch_ids = all_ids[i : i + batch_size]
            batch_metas = all_metas[i : i + batch_size]

            embeddings = self.embedder.embed(batch_docs)
            if embeddings is None:
                logger.error("embedding failed at rune batch %d", i)
                continue

            self.store._runes.add(
                ids=batch_ids,
                embeddings=embeddings,
                documents=batch_docs,
                metadatas=batch_metas,
            )

        logger.info("ingested %d rune entries", self.store._runes.count())

    # ---------- 召唤师技能 ----------

    def ingest_summoner_spells(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        spells_path = os.path.join(data_dir, "summoner_spells.json")
        if not os.path.exists(spells_path):
            logger.warning("summoner_spells.json not found at %s, run data_fetcher first", spells_path)
            return

        with open(spells_path, "r", encoding="utf-8") as f:
            spells = json.load(f)

        if not spells:
            return

        self._rebuild_collection("lol_summoner_spells", "_summoner_spells")

        docs = []
        ids = []
        metas = []

        for spell in spells:
            name = spell.get("name", "")
            key = spell.get("id", "")
            desc = spell.get("description", "")
            cd = spell.get("cooldownBurn", "")

            doc = f"Summoner Spell: {name}. {desc}. Cooldown: {cd}s."
            docs.append(doc)
            ids.append(f"spell_{key}")
            metas.append({
                "name": name,
                "cooldown": cd,
                "type": "summoner_spell",
            })

        embeddings = self.embedder.embed(docs)
        if embeddings is None:
            logger.error("embedding failed for summoner spells")
            return

        self.store._summoner_spells.add(
            ids=ids,
            embeddings=embeddings,
            documents=docs,
            metadatas=metas,
        )

        logger.info("ingested %d summoner spells", self.store._summoner_spells.count())

    # ---------- 游戏通用信息 ----------

    def ingest_game_info(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        game_info_path = os.path.join(data_dir, "game_info.json")
        if not os.path.exists(game_info_path):
            logger.warning("game_info.json not found at %s", game_info_path)
            return

        with open(game_info_path, "r", encoding="utf-8") as f:
            entries = json.load(f)

        if not entries:
            return

        self._rebuild_collection("lol_game_info", "_game_info")

        docs = []
        ids = []
        metas = []

        for i, entry in enumerate(entries):
            category = entry.get("category", "general")
            name = entry.get("name", "")
            desc = entry.get("description", "")

            doc = f"[{category.upper()}] {name}: {desc}"
            docs.append(doc)
            ids.append(f"game_info_{category}_{i}")
            metas.append({
                "name": name,
                "category": category,
                "type": "game_info",
            })

        embeddings = self.embedder.embed(docs)
        if embeddings is None:
            logger.error("embedding failed for game info")
            return

        self.store._game_info.add(
            ids=ids,
            embeddings=embeddings,
            documents=docs,
            metadatas=metas,
        )

        logger.info("ingested %d game info entries", self.store._game_info.count())

    # ---------- 工具方法 ----------

    def _rebuild_collection(self, collection_name: str, attr_name: str):
        """删除并重建指定 Collection。"""
        try:
            self.store.client.delete_collection(collection_name)
        except Exception:
            pass
        new_col = self.store.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        setattr(self.store, attr_name, new_col)

    @staticmethod
    def _split_sections(text: str) -> list[dict]:
        """按 ## 标题分块，每个块保留标题和内容。"""
        sections = []
        current_heading = "Overview"
        current_body = []
        heading_counter: dict[str, int] = {}
        for line in text.split("\n"):
            line = line.strip()
            # 跳过顶级标题（以 # 开头但不是 ##），不归入任何段
            if line.startswith("# ") and not line.startswith("## "):
                continue
            if line.startswith("## "):
                if current_body:
                    slug = re.sub(r"[^a-z0-9_]", "", current_heading.lower().replace(" ", "_"))
                    slug = slug or "section"
                    # 去重：同一 champion 下同 heading 加后缀
                    if slug in heading_counter:
                        heading_counter[slug] += 1
                        slug = f"{slug}_{heading_counter[slug]}"
                    else:
                        heading_counter[slug] = 0
                    sections.append({
                        "heading": current_heading,
                        "slug": slug,
                        "body": " ".join(current_body),
                        "phase": Ingestor._classify_phase(current_heading),
                    })
                current_heading = line[3:].strip()
                current_body = []
            else:
                current_body.append(line)
        if current_body:
            slug = re.sub(r"[^a-z0-9_]", "", current_heading.lower().replace(" ", "_"))
            slug = slug or "section"
            if slug in heading_counter:
                heading_counter[slug] += 1
                slug = f"{slug}_{heading_counter[slug]}"
            sections.append({
                "heading": current_heading,
                "slug": slug,
                "body": " ".join(current_body),
                "phase": Ingestor._classify_phase(current_heading),
            })
        return sections

    @staticmethod
    def _classify_phase(heading: str) -> str:
        h = heading.lower()
        if any(w in h for w in ("early", "laning", "lane", "0-14", "early game")):
            return "early"
        if any(w in h for w in ("mid", "15-25", "mid game", "roam")):
            return "mid"
        if any(w in h for w in ("late", "25+", "late game", "end game")):
            return "late"
        return "meta"


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


class GuideGenerator:
    """基于英雄数据和角色模板，自动生成结构化攻略。

    使用 Data Dragon 的：
    - tags (角色分类) → 选择角色模板
    - allytips → "使用技巧"
    - enemytips → "对抗技巧"
    - spells → "技能连招"
    - info → 难度和属性雷达
    """

    # ── 角色分类映射 ──
    TAG_ROLE_MAP = {
        "Assassin": "刺客",
        "Fighter": "战士",
        "Mage": "法师",
        "Marksman": "射手",
        "Support": "辅助",
        "Tank": "坦克",
    }

    # ── 各角色通用策略模板 ──
    ROLE_STRATEGY: dict[str, dict[str, str]] = {
        "战士": {
            "early": (
                "对线期利用技能消耗和回复优势压制对手。战士通常有不错的续航和持续伤害，"
                "可以积极换血后利用技能回复。注意控线——推线过深容易被 gank。"
                "三级是全盛期，多数战士拥有基础连招。利用草丛取消小兵仇恨来偷A。"
                "6级后单杀能力大幅提升，找角度打击对手是关键。"
            ),
            "mid": (
                "中期是战士最强时期。参与小龙团和先锋团，战士在2-3件套时又肉又有输出。"
                "注意进场时机——别第一个冲，等敌方关键控制交出后从侧翼切入。"
                "分带有优势时给边线压力，让对手疲于防守。利用传送支援远距离团战。"
                "保持经济领先，战士装备贵但滚雪球效果显著。"
            ),
            "late": (
                "后期团战需要精准判断——你是前排但非纯坦克。优先切敌方 ADC/法师，"
                "但不要脱离队友太远。保护己方后排时，利用控制技能拦截敌方突进。"
                "装备成型后，分带可以牵制多人，为队友创造以多打少机会。"
                "关键时刻 CD 很宝贵，等关键技能冷却完毕再进场。"
            ),
        },
        "坦克": {
            "early": (
                "坦克前期伤害不高但坦度足。专注补刀发育，必要时用技能清兵保塔。"
                "利用控制技能配合打野 gank，你的强控是击杀的关键。"
                "做好视野防 gank，坦度优势不代表可以浪。被动叠加双抗/护盾时再换血。"
                "TP 用来支援下路或者回线，不要浪费在对线消耗上。"
            ),
            "mid": (
                "中期是坦克发挥作用的核心时期。配合队友抓单，你的控制链是开启战斗的信号。"
                "小龙和先锋团你是前排核心——站在队伍最前吸收伤害。"
                "保护我方核心输出，用技能打断敌方突进。好的坦克能决定团战节奏。"
                "必要时牺牲自己掩护队友撤退，坦克的死值得。"
            ),
            "late": (
                "后期团战非常依靠你的先手。找到机会打出关键开团后，不要追击——回来保护队友。"
                "你的技能CD在后期很短，持续给出控制。盯紧敌方核心输出位置。"
                "装备成型后你是最硬的盾。正面站住，让队友在安全范围内输出。"
                "注意兵线——作为坦克也可以推线给对手压力。"
            ),
        },
        "刺客": {
            "early": (
                "前期尽量补刀发育，刺客在基础技能全之前伤害不足。用技能消耗耗血，"
                "找机会打出血线压制。注意你的技能冷却——刺客靠一套爆发。"
                "对方血量低于60%时开始寻找击杀机会。利用草丛和视野盲区施压。"
                "6级后是质变点——大部分刺客有单杀或强游走能力。推线后伺机游走。"
            ),
            "mid": (
                "中期是刺客的黄金期。推完线后频繁游走边路，蹲草抓人是你的强项。"
                "捕捉敌方走位失误——脆皮落单就是你的猎物。先手一套秒杀立即脱离。"
                "团战前尝试在敌方视野外找角度切入。盯着对面最脆的人。"
                "经济落后时不要硬打，找落单目标收人头补发育。"
            ),
            "late": (
                "后期团战难度增大。敌方抱团后单切风险很高，需要更好的时机判断。"
                "保持侧翼或后方站位，等队友先开团再用一套带走关键目标。"
                "分带也是一种策略——对手少人开团会犹豫，你来带线施压。"
                "注意金身/复活甲等保命装备的存在，算好敌方关键技能CD。"
                "活着才有输出，不要换一条命——刺客的死通常不值得。"
            ),
        },
        "法师": {
            "early": (
                "法师前期重视补刀和蓝量管理。利用技能消耗同时保持安全的距离。"
                "大多数法师前期较弱，避免无意义换血。利用平A补刀节省蓝量。"
                "注意对方打野位置——法师缺乏逃生能力。六级前后是重要分水岭。"
                "带传送或净化根据对局选择，保命优先。"
            ),
            "mid": (
                "中期法师拥有可观的清线和消耗能力。快速清兵后支援边路。"
                "团战站位靠后——你是输出核心但极脆。利用技能射程在安全线外输出。"
                "注意控制技能的释放——关键控制可以决定团战走向。"
                "关注小龙和先锋团时间，提前用技能在龙坑消耗对手。"
            ),
            "late": (
                "后期法师伤害爆炸。团战前用技能消耗对手血量，但不要贪——保留位移或控制自保。"
                "站后面输出，让你的前排给你创造空间。注意敌方刺客/突进位置。"
                "金身是你的救命装备，关键时刻用。任何控制打到你可能就GG。"
                "你的AOE输出是团战胜负关键，找准角度放技能。"
            ),
        },
        "射手": {
            "early": (
                "ADC 前期以发育为核心。认真补好每一刀，保持兵线在安全位置。"
                "辅助负责消耗，你负责输出——但别贸然跟辅助上头。"
                "注意小地图信息，敌方打野和中路消失时立即后撤。"
                "第一个大件前伤害不显，不要主动求战。经济是ADC的生命线。"
            ),
            "mid": (
                "中期 ADC 开始发力。拿到一两件装备后伤害可观。配合队友推进外塔和拿龙。"
                "团战站位是核心——永远在辅助和坦克身后输出。活着就有输出。"
                "注意对方切入路线，保留闪现/位移技能自保。你的死可能是团灭开端。"
                "推完塔后转线继续施压，不要独自深入敌区。"
            ),
            "late": (
                "后期 ADC 是团队最强输出点。每一步走位都关键——失误即死。"
                "团战优先打离你最近的目标，不要冲到前线去打后排。"
                "出水银饰带/复活甲增加容错。注意对方关键技能是否已交。"
                "优势时跟团推进，劣势时守塔清兵拖延。你的持续输出无人能及。"
            ),
        },
        "辅助": {
            "early": (
                "前期辅助负责视野控制和消耗。帮 ADC 创造安全的补刀环境。"
                "在河道关键位置插眼，掌握敌方打野动向。利用技能消耗对手 AD。"
                "控制好兵线——辅助不要乱 A 兵，让 ADC 控线。"
                "注意小地图给队友打信号，你是团队的眼睛。"
            ),
            "mid": (
                "中期辅助开始游走。帮打野控制野区视野，游走中路施压。"
                "小龙团和先锋团提前做视野，掌控关键区域。你的控制技能是团战发动机。"
                "保护 ADC 和 AP 是你的首要任务。牺牲自己成全队友。"
                "出团队装备（骑士之誓/救赎）增加团队价值。"
            ),
            "late": (
                "后期辅助的视野决定团战走向。提前在关键路口布控——谁有视野谁赢。"
                "团战时紧盯我方核心输出，用技能保护他们。你的价值在于让队友活下来。"
                "注意自己的站位——辅助也怕被秒。关键时刻用自己换 ADC 的命。"
                "购买控制守卫（真眼）保持视野压制，清掉敌方视野。"
            ),
        },
    }

    # ── 伤害类型 → 出装建议 ──
    DAMAGE_BUILD_TIPS: dict[str, dict[str, str]] = {
        "AD": {
            "Fighter": "黑切、血手、死亡之舞是战士核心装。对线 AD 先出布甲鞋，对线 AP 出水银鞋。贪欲九头蛇提供清线和续航。",
            "Assassin": "幽梦、幕刃、夜之锋刃是刺客核心。暗行者之爪提供额外突进。赛瑞尔达的怨恨破甲。",
            "Marksman": "海妖杀手、无尽之刃、多米尼克领主的致意是射手核心。绿叉/饮血提供保命。",
        },
        "AP": {
            "Mage": "卢登的伙伴/兰德里的苦楚看对方阵容。影焰、灭世者的死亡之帽、虚空之杖是法师核心。中娅沙漏提供团战保命。",
            "Assassin": "暗夜收割者提供爆发。巫妖之祸提供普攻伤害。中娅沙漏提供进场容错。",
        },
    }

    def generate(self, champ: dict) -> list[dict]:
        """为单个英雄生成攻略段列表。"""
        name = champ.get("name", "Unknown")
        tags = champ.get("tags", [])
        partype = champ.get("partype", "")
        info = champ.get("info", {})
        lore = champ.get("lore", "")
        blurb = champ.get("blurb", "")
        allytips = champ.get("allytips", [])
        enemytips = champ.get("enemytips", [])
        spells = champ.get("spells", [])
        passive = champ.get("passive", {})
        stats = champ.get("stats", {})

        role = self._classify_role(tags)
        sections = []

        # 1. 英雄概览
        sections.append(self._overview(name, tags, info, lore, blurb, stats, passive, spells, role))

        # 2-4. 早中晚期策略
        strategy = self.ROLE_STRATEGY.get(role, self.ROLE_STRATEGY["战士"])
        sections.append({"heading": "Early Game Strategy (0-14 min)", "body": strategy["early"],
                         "slug": "early_game_strategy", "phase": "early"})
        sections.append({"heading": "Mid Game Strategy (15-25 min)", "body": strategy["mid"],
                         "slug": "mid_game_strategy", "phase": "mid"})
        sections.append({"heading": "Late Game Strategy (25+ min)", "body": strategy["late"],
                         "slug": "late_game_strategy", "phase": "late"})

        # 5. 技能连招
        sections.append(self._skill_combos(name, passive, spells, role))

        # 6. 使用技巧（来自 Data Dragon allytips）
        if allytips:
            sections.append({"heading": "How to Play (Tips)", "body": " ".join(allytips),
                             "slug": "how_to_play", "phase": "meta"})

        # 7. 对抗技巧（来自 Data Dragon enemytips）
        if enemytips:
            sections.append({"heading": "How to Counter (Enemy Tips)", "body": " ".join(enemytips),
                             "slug": "how_to_counter", "phase": "meta"})

        # 8. 出装建议
        sections.append(self._build_recommendations(tags, role, spells, partype))

        # 9. 关键数据
        sections.append(self._key_stats(name, stats))

        return sections

    def _classify_role(self, tags: list[str]) -> str:
        for tag in tags:
            if tag in self.TAG_ROLE_MAP:
                return self.TAG_ROLE_MAP[tag]
        return "战士"

    def _overview(self, name: str, tags: list[str], info: dict, lore: str,
                  blurb: str, stats: dict, passive: dict, spells: list[dict],
                  role: str) -> dict:
        parts = [f"{name} 是一个{role}英雄"]
        if tags:
            parts.append(f"定位：{'/'.join(tags)}")
        if blurb:
            parts.append(blurb)
        if info:
            parts.append(
                f"难度{info.get('difficulty',0)}/10，"
                f"攻击{info.get('attack',0)}/10，"
                f"防御{info.get('defense',0)}/10，"
                f"法术{info.get('magic',0)}/10"
            )
        # 被动技能
        if passive and passive.get("name"):
            parts.append(f"被动：{passive.get('name', '')}")
        # 技能名列表
        spell_names = [s.get("name", "") for s in spells if s.get("name")]
        if spell_names:
            parts.append(f"技能：Q-{spell_names[0] if len(spell_names)>0 else ''} "
                         f"W-{spell_names[1] if len(spell_names)>1 else ''} "
                         f"E-{spell_names[2] if len(spell_names)>2 else ''} "
                         f"R-{spell_names[3] if len(spell_names)>3 else ''}")
        return {
            "heading": "Overview",
            "body": "；".join(parts),
            "slug": f"auto_overview",
            "phase": "meta",
        }

    def _skill_combos(self, name: str, passive: dict, spells: list[dict], role: str) -> dict:
        spell_names = [s.get("name", "") for s in spells if s.get("name")]
        passive_name = passive.get("name", "") if passive else ""

        combos = []
        if role in ("刺客", "法师"):
            combos.append("常规消耗连招：利用基础技能进行 poke，压低血线后找机会")
            if len(spell_names) >= 4:
                combos.append(f"爆发连招：{spell_names[0]} → {spell_names[1]} → {spell_names[2]} → {spell_names[3]} 一套带走")
            if passive_name:
                combos.append(f"注意触发被动 {passive_name} 来最大化伤害")
        elif role in ("战士", "坦克"):
            if len(spell_names) >= 4:
                combos.append(f"标准连招：{spell_names[0]} → {spell_names[1]} → {spell_names[2]}插入普攻 → {spell_names[3]}")
            combos.append("利用技能间隙穿插普攻以最大化输出")
            if passive_name:
                combos.append(f"保持被动 {passive_name} 层数以获得增益效果")
        elif role == "射手":
            combos.append("走A是基本功——每发普攻之间移动来保持安全距离")
            if passive_name:
                combos.append(f"留意被动 {passive_name} 的触发条件，最大化攻速/伤害加成")
        elif role == "辅助":
            combos.append("注意技能释放时机，配合 ADC 打出关键控制链")
            if passive_name:
                combos.append(f"利用被动 {passive_name} 给队友提供额外增益")

        if not combos:
            combos.append("熟练掌握基础技能连招，注意技能释放顺序和时机")

        return {
            "heading": "Skill Combos",
            "body": " ".join(combos),
            "slug": "auto_skill_combos",
            "phase": "meta",
        }

    def _build_recommendations(self, tags: list[str], role: str,
                                spells: list[dict], partype: str) -> dict:
        tips = []
        is_ad = self._is_ad_champ(tags, spells, partype)

        if is_ad:
            for sub_role in tags:
                if sub_role in self.DAMAGE_BUILD_TIPS.get("AD", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AD"][sub_role])
                    break
            else:
                if role in self.DAMAGE_BUILD_TIPS.get("AD", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AD"].get(role, "根据对局情况选择核心输出装备"))
        else:
            for sub_role in tags:
                if sub_role in self.DAMAGE_BUILD_TIPS.get("AP", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AP"][sub_role])
                    break
            else:
                if role in self.DAMAGE_BUILD_TIPS.get("AP", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AP"].get(role, "选择法术强度或法术穿透装备"))

        if role == "坦克":
            tips.append("出装建议：日炎圣盾、荆棘之甲、自然之力是坦克核心。根据对方主要输出类型选择护甲或魔抗。石像鬼石板甲提供团战无敌。")
        elif role == "辅助":
            tips.append("辅助出装：升级辅助装 → 骑士之誓/救赎 → 警觉眼石。根据对方阵容选择鸟盾/坩埚。控制守卫（真眼）常备2个。")

        return {
            "heading": "Build Recommendations",
            "body": " ".join(tips) if tips else "根据对局和阵容灵活选择出装。",
            "slug": "auto_build",
            "phase": "meta",
        }

    def _is_ad_champ(self, tags: list[str], spells: list[dict], partype: str) -> bool:
        """粗略判断 AD/AP。"""
        if "Marksman" in tags:
            return True
        if "Fighter" in tags:
            return True
        if "Tank" in tags:
            return True
        if "Assassin" in tags:
            # 有些刺客是 AP (如阿卡丽、艾克)，但大部分是 AD
            return True
        return False

    def _key_stats(self, name: str, stats: dict) -> dict:
        stat_info = []
        key_stats_map = {
            "hp": "基础生命值", "hpperlevel": "生命成长", "mp": "基础法力值", "mpperlevel": "法力成长",
            "movespeed": "移速", "attackrange": "攻击距离",
            "armor": "基础护甲", "armorperlevel": "护甲成长",
            "spellblock": "基础魔抗", "spellblockperlevel": "魔抗成长",
            "attackdamage": "基础攻击力", "attackdamageperlevel": "攻击成长",
            "attackspeed": "攻速", "attackspeedperlevel": "攻速成长",
        }
        for k, label in key_stats_map.items():
            if k in stats:
                stat_info.append(f"{label}: {stats[k]}")
        return {
            "heading": "Key Stats",
            "body": " ".join(stat_info),
            "slug": "auto_key_stats",
            "phase": "meta",
        }


if __name__ == "__main__":
    ingestor = Ingestor()
    ingestor.ingest_all()
    logger.info("ingest complete")
