"""一次性数据摄入脚本。运行方式: python -m knowledge.ingest"""

import json
import logging
import os

from knowledge.chroma_store import ChromaStore
from knowledge.embedder import Embedder

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class Ingestor:
    """将装备 JSON 和英雄攻略 Markdown 嵌入 ChromaDB."""

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
        self.ingest_guides()

    def ingest_items(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        items_path = os.path.join(data_dir, "items.json")
        if not os.path.exists(items_path):
            logger.warning("items.json not found at %s", items_path)
            return

        with open(items_path, "r", encoding="utf-8") as f:
            items = json.load(f)

        if not items:
            return

        # 清空重建
        try:
            self.store.client.delete_collection("lol_items")
            self.store._items = self.store.client.get_or_create_collection(
                name="lol_items",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            pass

        batch_size = 50
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            docs = []
            ids = []
            metas = []
            for item in batch:
                doc = ItemFormatter.format(item)
                docs.append(doc)
                ids.append(f"item_{item['id']}")
                metas.append({
                    "name": item.get("name", ""),
                    "cost": item.get("cost", 0),
                    "tags": ",".join(item.get("tags", [])),
                    "type": "item",
                })

            embeddings = self.embedder.embed(docs)
            if embeddings is None:
                logger.error("embedding failed at batch %d", i)
                continue

            self.store._items.add(
                ids=ids,
                embeddings=embeddings,
                documents=docs,
                metadatas=metas,
            )

        logger.info("ingested %d items", self.store._items.count())

    def ingest_guides(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data", "champions")
        if not os.path.exists(data_dir):
            logger.warning("champions dir not found at %s", data_dir)
            return

        try:
            self.store.client.delete_collection("lol_champion_guides")
            self.store._guides = self.store.client.get_or_create_collection(
                name="lol_champion_guides",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            pass

        for filename in os.listdir(data_dir):
            if not filename.endswith(".md"):
                continue
            champion = filename[:-3]  # remove .md
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

        logger.info("ingested %d guide chunks", self.store._guides.count())

    @staticmethod
    def _split_sections(text: str) -> list[dict]:
        """按 ## 标题分块，每个块保留标题和内容."""
        sections = []
        current_heading = "Overview"
        current_body = []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("## "):
                if current_body:
                    heading_text = current_body[0].strip() if current_body else current_heading
                    body_text = " ".join(current_body)
                    sections.append({
                        "heading": current_heading,
                        "slug": current_heading.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(":", ""),
                        "body": body_text,
                        "phase": Ingestor._classify_phase(current_heading),
                    })
                current_heading = line[3:].strip()
                current_body = []
            else:
                current_body.append(line)
        # last section
        if current_body:
            sections.append({
                "heading": current_heading,
                "slug": current_heading.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(":", ""),
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
    """将装备 JSON 转为可供 embedding 的自然语言描述."""

    @staticmethod
    def format(item: dict) -> str:
        name = item.get("name", "Unknown Item")
        plaintext = item.get("plaintext", "")
        cost = item.get("cost", 0)
        tags = item.get("tags", [])
        stats = item.get("stats", {})
        from_items = item.get("from", [])
        into_items = item.get("into", [])

        parts = [f"{name}: {plaintext}"]
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


if __name__ == "__main__":
    ingestor = Ingestor()
    ingestor.ingest_all()
    logger.info("ingest complete")
