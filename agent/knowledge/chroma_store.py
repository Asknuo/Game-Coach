import logging
import os

import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)


class ChromaStore:
    """ChromaDB 向量存储封装，管理多个 Collection:

    - lol_items:          装备数据
    - lol_champion_guides: 英雄攻略
    - lol_champions:       英雄技能数据（来自 Data Dragon）
    - lol_runes:          符文系统
    - lol_summoner_spells: 召唤师技能
    - lol_game_info:       游戏机制/野怪/地图等通用信息
    """

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
            self._champions = None
            self._runes = None
            self._summoner_spells = None
            self._game_info = None
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
            self._champions = self.client.get_or_create_collection(
                name="lol_champions",
                metadata={"hnsw:space": "cosine"},
            )
            self._runes = self.client.get_or_create_collection(
                name="lol_runes",
                metadata={"hnsw:space": "cosine"},
            )
            self._summoner_spells = self.client.get_or_create_collection(
                name="lol_summoner_spells",
                metadata={"hnsw:space": "cosine"},
            )
            self._game_info = self.client.get_or_create_collection(
                name="lol_game_info",
                metadata={"hnsw:space": "cosine"},
            )
            self.available = True
        except Exception:
            logger.exception("ChromaDB collection init failed")
            self.available = False

    def needs_refresh(self) -> bool:
        """检查知识库是否需要刷新（无数据 或 超过 7 天未更新）."""
        import time
        from pathlib import Path
        stamp_file = Path(self.persist_dir) / ".last_ingest"
        if not stamp_file.exists():
            return True
        try:
            elapsed = time.time() - stamp_file.stat().st_mtime
            if elapsed > 7 * 86400:
                return True
        except Exception:
            return True
        try:
            if self._items and self._items.count() == 0:
                return True
        except Exception:
            return True
        return False

    def mark_ingested(self):
        """记录知识库更新时间戳."""
        from pathlib import Path
        stamp_file = Path(self.persist_dir) / ".last_ingest"
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text("")

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

    @property
    def champions(self):
        if not self.available:
            return None
        return self._champions

    @property
    def runes(self):
        if not self.available:
            return None
        return self._runes

    @property
    def summoner_spells(self):
        if not self.available:
            return None
        return self._summoner_spells

    @property
    def game_info(self):
        if not self.available:
            return None
        return self._game_info
