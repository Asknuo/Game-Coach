import json
import logging
import os
from urllib.request import urlopen, Request

logger = logging.getLogger(__name__)


class Embedder:
    """Embedding 客户端。

    支持两种后端：
    - OpenAI 兼容 API（默认 text-embedding-3-small，通过 OpenAI SDK）
    - 火山引擎豆包 Doubao Embedding（通过原生 HTTP）

    通过环境变量配置：
    - EMBEDDING_API_KEY   → API Key
    - EMBEDDING_BASE_URL  → 自定义端点（火山引擎填完整 URL）
    - EMBEDDING_MODEL     → 模型名称（默认 text-embedding-3-small）

    火山引擎示例：
        EMBEDDING_API_KEY=5a5dc2a6-...
        EMBEDDING_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal
        EMBEDDING_MODEL=doubao-embedding-vision-251215
    """

    def __init__(self):
        self.model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self.api_key = (
            os.getenv("EMBEDDING_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")   # fallback
            or os.getenv("LLM_API_KEY", "")       # 最后 fallback
        )
        self.base_url = os.getenv("EMBEDDING_BASE_URL", "")
        self._client = None
        self._backend = "openai"  # "openai" or "volces"
        self.available = False

        if not self.api_key:
            logger.warning(
                "No embedding API key found (set EMBEDDING_API_KEY, OPENAI_API_KEY, or LLM_API_KEY)"
            )
            return

        # 检测火山引擎
        if self.base_url and ("volces" in self.base_url or "ark.cn" in self.base_url):
            self._backend = "volces"
            self._endpoint = self.base_url
            self.available = True
            logger.debug("Embedder: Volces/Doubao backend, model=%s", self.model)
        else:
            try:
                from openai import OpenAI

                kwargs = {"api_key": self.api_key}
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._client = OpenAI(**kwargs)
                self.available = True
                logger.debug("Embedder: OpenAI backend, model=%s", self.model)
            except Exception:
                logger.exception("OpenAI Embedder init failed")

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.available:
            return None

        if self._backend == "volces":
            return self._embed_volces(texts)
        else:
            return self._embed_openai(texts)

    def embed_query(self, text: str) -> list[float] | None:
        result = self.embed([text])
        if result:
            return result[0]
        return None

    # ── OpenAI 后端 ──

    def _embed_openai(self, texts: list[str]) -> list[list[float]] | None:
        if not self._client:
            return None
        try:
            resp = self._client.embeddings.create(
                model=self.model,
                input=texts,
            )
            return [d.embedding for d in resp.data]
        except Exception:
            logger.exception("OpenAI embedding failed")
            return None

    # ── 火山引擎豆包后端 ──

    def _embed_volces(self, texts: list[str]) -> list[list[float]] | None:
        """使用火山引擎 Doubao Embedding API 做文本嵌入。

        豆包 multimodal API 对批量输入只返回单个 embedding，
        因此逐条发送，通过线程池并发加速。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = min(len(texts), 10)
        results: dict[int, list[float]] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._embed_volces_single, t): i for i, t in enumerate(texts)}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    emb = future.result()
                    if emb is None:
                        logger.error("Volces embedding failed for text %d", idx)
                        return None
                    results[idx] = emb
                except Exception:
                    logger.exception("Volces embedding failed for text %d", idx)
                    return None

        return [results[i] for i in range(len(texts))]

    @staticmethod
    def _parse_volces_embedding(data: dict) -> list[float] | None:
        """从火山引擎响应 JSON 中提取 embedding 向量。"""
        result_data = data.get("data", {})
        if isinstance(result_data, dict):
            emb = result_data.get("embedding")
            if emb is not None:
                return emb
        elif isinstance(result_data, list):
            for item in result_data:
                if isinstance(item, dict):
                    emb = item.get("embedding")
                    if emb is not None:
                        return emb
        return data.get("embedding")

    def _embed_volces_single(self, text: str) -> list[float] | None:
        """发送单条文本获取 embedding。"""
        payload = json.dumps({
            "model": self.model,
            "input": [{"type": "text", "text": text}],
        }).encode("utf-8")

        try:
            req = Request(
                self._endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            with urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
        except Exception:
            logger.exception("Volces embedding request failed")
            return None

        try:
            data = json.loads(raw)
            emb = self._parse_volces_embedding(data)
            if emb is not None:
                return emb
            logger.warning("Volces: unexpected response format, raw=%s", raw[:500])
            return None
        except Exception:
            logger.exception("Volces embedding parse failed, raw=%s", raw[:500])
            return None
