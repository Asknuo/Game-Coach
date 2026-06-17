import logging
import os

logger = logging.getLogger(__name__)


class Embedder:
    """Embedding 客户端。

    支持 OpenAI Embedding API（默认 text-embedding-3-small，1536 维）。
    DeepSeek 暂无 Embedding API，embedding 侧继续使用 OpenAI。
    可通过 EMBEDDING_API_KEY 和 EMBEDDING_BASE_URL 配置独立端点。

    若未配置 API Key，embedding 功能降级不可用，
    Retriever 返回空结果，Skills 回退模板原文。
    """

    def __init__(self):
        self.model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self.api_key = (
            os.getenv("EMBEDDING_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")       # fallback
            or os.getenv("LLM_API_KEY", "")           # 最后 fallback
        )
        self.base_url = os.getenv("EMBEDDING_BASE_URL", "")
        self._client = None
        self.available = False

        if self.api_key:
            try:
                from openai import OpenAI

                kwargs = {"api_key": self.api_key}
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._client = OpenAI(**kwargs)
                self.available = True
            except Exception:
                logger.exception("Embedder init failed")
        else:
            logger.warning(
                "No embedding API key found (set EMBEDDING_API_KEY or OPENAI_API_KEY)"
            )

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.available or not self._client:
            return None
        try:
            resp = self._client.embeddings.create(
                model=self.model,
                input=texts,
            )
            return [d.embedding for d in resp.data]
        except Exception:
            logger.exception("embedding failed")
            return None

    def embed_query(self, text: str) -> list[float] | None:
        result = self.embed([text])
        if result:
            return result[0]
        return None
