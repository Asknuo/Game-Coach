"""摄入流水线 — 将 Data Dragon 数据嵌入 ChromaDB。运行方式: python -m knowledge.ingest

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
from knowledge.ingest.formatters import ChampionFormatter, ItemFormatter
from knowledge.ingest.guide_generator import GuideGenerator

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# 缓存数据目录（knowledge/data/），与包位置解耦
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


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
        items_path = os.path.join(_DATA_DIR, "items.json")
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
        champions_path = os.path.join(_DATA_DIR, "champions.json")
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
        data_dir = os.path.join(_DATA_DIR, "champions")
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
        champions_path = os.path.join(_DATA_DIR, "champions.json")
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
        runes_path = os.path.join(_DATA_DIR, "runes.json")
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
        spells_path = os.path.join(_DATA_DIR, "summoner_spells.json")
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
        game_info_path = os.path.join(_DATA_DIR, "game_info.json")
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
