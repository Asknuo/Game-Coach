"""services/lifecycle.py — 知识库后台摄入回归测试."""

from types import SimpleNamespace

import pytest

from services import lifecycle


@pytest.mark.asyncio
async def test_bg_ingest_reuses_main_store(monkeypatch):
    """B1 回归：后台摄入必须复用主进程 store/embedder，
    不得再建第二个 PersistentClient（会删建主进程正持有的 collection）."""
    captured: dict = {}

    class FakeIngestor:
        def __init__(self, store=None, embedder=None):
            captured["store"] = store
            captured["embedder"] = embedder

        def ingest_all(self):
            captured["ran"] = True

    import knowledge.ingest as ingest_pkg

    monkeypatch.setattr(ingest_pkg, "Ingestor", FakeIngestor)

    store, embedder = object(), object()
    ctx = SimpleNamespace(
        retriever=SimpleNamespace(store=store, embedder=embedder),
    )
    await lifecycle.bg_ingest(ctx)  # type: ignore[arg-type]

    assert captured["store"] is store
    assert captured["embedder"] is embedder
    assert captured.get("ran") is True
