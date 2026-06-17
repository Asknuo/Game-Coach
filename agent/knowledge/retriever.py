import logging

from knowledge.chroma_store import ChromaStore
from knowledge.embedder import Embedder

logger = logging.getLogger(__name__)

_instance: "Retriever | None" = None


def set_retriever(r: "Retriever") -> None:
    """由 app.py 启动时注入，供所有 Skill 模块通过 get_retriever() 获取."""
    global _instance
    _instance = r


def get_retriever() -> "Retriever | None":
    return _instance


class Retriever:
    """统一检索接口，被 Skills 调用."""

    def __init__(self, store: ChromaStore, embedder: Embedder):
        self.store = store
        self.embedder = embedder

    @property
    def available(self) -> bool:
        return self.store.available and self.embedder.available

    def search_items(self, query: str, n: int = 3) -> list[dict]:
        """语义搜索装备。"""
        if not self.available or self.store.items is None:
            return []
        emb = self.embedder.embed_query(query)
        if emb is None:
            return []
        try:
            results = self.store.items.query(
                query_embeddings=[emb],
                n_results=n,
            )
            out = []
            if results["documents"] and results["documents"][0]:
                for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                    out.append({"document": doc, "metadata": meta})
            return out
        except Exception:
            logger.exception("item search failed")
            return []

    def search_guide(
        self,
        champion: str,
        query: str,
        phase: str | None = None,
        n: int = 3,
    ) -> list[dict]:
        """按英雄名 + 可选阶段过滤的攻略检索。"""
        if not self.available or self.store.guides is None:
            return []
        emb = self.embedder.embed_query(query)
        if emb is None:
            return []
        where: dict = {"champion": champion}
        if phase:
            where["phase"] = phase
        try:
            results = self.store.guides.query(
                query_embeddings=[emb],
                n_results=n,
                where=where,
            )
            out = []
            if results["documents"] and results["documents"][0]:
                for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                    out.append({"document": doc, "metadata": meta})
            return out
        except Exception:
            logger.exception("guide search failed for champion=%s", champion)
            return []

    def search_guide_by_time(
        self,
        champion: str,
        game_time: float,
        query: str,
        n: int = 3,
    ) -> list[dict]:
        """根据游戏时间自动推断阶段，然后检索攻略。"""
        if game_time < 14 * 60:
            phase = "early"
        elif game_time < 25 * 60:
            phase = "mid"
        else:
            phase = "late"
        return self.search_guide(champion, query, phase=phase, n=n)
