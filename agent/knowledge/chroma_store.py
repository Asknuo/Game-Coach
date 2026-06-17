import logging
import os

import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)


class ChromaStore:
    """ChromaDB 向量存储封装，管理 items 和 champion_guides 两个 Collection."""

    def __init__(self, persist_dir: str | None = None):
        if persist_dir is None:
            persist_dir = os.path.join(os.path.dirname(__file__), "chroma_data")
        self.persist_dir = persist_dir

        try:
            self.client = chromadb.PersistentClient(
                path=persist_dir,
                settings=Settings(anonymized_telemetry=False),
            )
            self.available = True
        except Exception:
            logger.exception("ChromaDB init failed, vector search disabled")
            self.client = None
            self.available = False
            self._items = None
            self._guides = None
            return

        try:
            self._items = self.client.get_or_create_collection(
                name="lol_items",
                metadata={"hnsw:space": "cosine"},
            )
            self._guides = self.client.get_or_create_collection(
                name="lol_champion_guides",
                metadata={"hnsw:space": "cosine"},
            )
            self.available = True
            logger.info(
                "ChromaDB ready: %d items, %d guide chunks",
                self._items.count(),
                self._guides.count(),
            )
        except Exception:
            logger.exception("ChromaDB collection init failed")
            self._items = None
            self._guides = None
            self.available = False

    @property
    def items(self):
        if not self.available:
            return None
        return self._items

    @property
    def guides(self):
        if not self.available:
            return None
        return self._guides
