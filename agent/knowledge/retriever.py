import logging

from knowledge.chroma_store import ChromaStore
from knowledge.embedder import Embedder

logger = logging.getLogger(__name__)


class Retriever:
    """统一检索接口，唯一入口是 aggregate_coaching_context（流水线 RAG 节点）。

    支持检索：
    - 装备 (items)
    - 英雄攻略 (guides)
    - 游戏通用信息 (game_info) — 按类别过滤
    """

    def __init__(self, store: ChromaStore, embedder: Embedder):
        self.store = store
        self.embedder = embedder

    @property
    def available(self) -> bool:
        return self.store.available and self.embedder.available

    # ---- 装备 ----

    def search_items(self, query: str, n: int = 3) -> list[dict]:
        """语义搜索装备。"""
        return self._search(self.store.items, query, n=n)

    # ---- 英雄攻略 ----

    def search_guide(
        self,
        champion: str,
        query: str,
        phase: str | None = None,
        n: int = 3,
    ) -> list[dict]:
        """按英雄名 + 可选阶段过滤的攻略检索。支持大小写不敏感匹配。"""
        # 尝试小写和首字母大写两种形式（兼容手动和自动攻略）
        where_list = [
            {"champion": champion},
            {"champion": champion.lower()},
            {"champion": champion.capitalize()},
        ]
        # 去重
        seen = set()
        unique_where = []
        for w in where_list:
            key = tuple(sorted(w.items()))
            if key not in seen:
                seen.add(key)
                unique_where.append(w)

        all_results = []
        for where in unique_where:
            w: dict = {"champion": where["champion"]}
            if phase:
                w["phase"] = phase
            results = self._search(self.store.guides, query, n=n * 2, where=w)
            all_results.extend(results)

        # 去重并按相似度截取（ChromaDB 按距离排序，前面的更相关）
        seen_docs = set()
        deduped = []
        for r in all_results:
            if r["document"] not in seen_docs:
                seen_docs.add(r["document"])
                deduped.append(r)
        return deduped[:n]

    # ---- 游戏通用信息 ----

    def search_game_info(
        self,
        query: str,
        category: str | None = None,
        n: int = 5,
    ) -> list[dict]:
        """语义搜索游戏通用信息。

        Args:
            query: 语义查询
            category: 可选，限制类别 (map/objective/monster/mechanic)
            n: 返回数量
        """
        where = None
        if category:
            where = {"category": category}
        return self._search(self.store.game_info, query, n=n, where=where)

    # ---- 聚合：多源知识整合为用户可读的教练建议 ----

    def aggregate_coaching_context(
        self,
        ally_champion: str,
        enemy_champion: str | None = None,
        game_time: float = 0,
        event_name: str = "",
        event_query: str = "",
        max_length: int = 500,
    ) -> str:
        """聚合多源知识，输出一段可直接呈现的教练建议文本。

        整合：
        1. 己方英雄攻略（按游戏时间分阶段）
        2. 敌方英雄攻略（对线信息）
        3.  事件相关游戏机制
        4. 必要时查询装备推荐

        返回：整合后的纯文本，适合直接显示或交给 LLM 二次润色。
        """
        if not self.available:
            return ""

        parts: list[str] = []

        # 推断游戏阶段
        phase: str | None = None
        if ally_champion and event_query:
            if game_time < 14 * 60:
                phase = "early"
            elif game_time < 25 * 60:
                phase = "mid"
            else:
                phase = "late"

        # 1. 己方英雄当前阶段攻略
        if ally_champion and event_query:
            guide = self.search_guide(ally_champion, event_query, phase=phase, n=2)
            for r in guide:
                doc = r["document"]
                # 去掉 "[Champion] heading:" 前缀，更干净
                if "] " in doc:
                    doc = doc.split("] ", 1)[-1]
                parts.append(doc)

        # 2. 敌方英雄信息 + 对线策略
        if enemy_champion and event_query:
            enemy_guide = self.search_guide(
                enemy_champion, f"matchup against {ally_champion} early game laning tips", n=2
            )
            for r in enemy_guide:
                doc = r["document"]
                if "] " in doc:
                    doc = doc.split("] ", 1)[-1]
                # 标记来源
                parts.append(f"Enemy {enemy_champion}: {doc}")

            # 特殊：查对抗技巧
            counter_tips = self._search(
                self.store.guides,
                f"{enemy_champion} enemy tips counter",
                n=1,
                where={"champion": enemy_champion},
            )
            for r in counter_tips:
                if "How to Counter" in r.get("metadata", {}).get("heading", ""):
                    doc = r["document"]
                    if "] " in doc:
                        doc = doc.split("] ", 1)[-1]
                    parts.append(f"Counter tip: {doc}")

        # 3. 事件相关游戏知识
        if event_name or event_query:
            info_query = event_query or event_name
            info = self.search_game_info(info_query, n=2)
            for r in info:
                doc = r["document"]
                if "] " in doc:
                    doc = doc.split("] ", 1)[-1]
                if doc not in parts:
                    parts.append(doc)

        # 4. 装备类事件附加装备建议
        if event_name in ("item_purchased", "item_sold", "item_upgraded", "enemy_item_purchased", "build_check") or "item" in event_query.lower():
            items = self.search_items(event_query or "recommended build core items", n=2)
            for r in items:
                doc = r["document"]
                if doc not in parts:
                    parts.append(doc)

        # 去重 + 截断
        seen = set()
        deduped = []
        for p in parts:
            key = p[:100]
            if key not in seen:
                seen.add(key)
                deduped.append(p)

        result = " | ".join(deduped)
        if len(result) > max_length:
            # 智能截断：保留完整句子
            result = result[:max_length].rsplit(".", 1)[0] + "."
        return result

    # ---- 内部方法 ----

    def _search(
        self,
        collection,
        query: str,
        n: int = 3,
        where: dict | None = None,
    ) -> list[dict]:
        if not self.available or collection is None:
            return []
        emb = self.embedder.embed_query(query)
        if emb is None:
            return []
        try:
            kwargs = {"query_embeddings": [emb], "n_results": n}
            if where:
                # ChromaDB 多字段 where 需要用 $and 包裹
                if len(where) > 1:
                    kwargs["where"] = {"$and": [{k: v} for k, v in where.items()]}
                else:
                    kwargs["where"] = where
            results = collection.query(**kwargs)
            out = []
            if results["documents"] and results["documents"][0]:
                for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                    out.append({"document": doc, "metadata": meta})
            return out
        except Exception:
            logger.exception("search failed")
            return []
