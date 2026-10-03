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


# ── 启动预热（bg_warmup / _warmup_sync） ────────────────

def _fake_retriever(champions, available=True):
    """预热测试桩：matchup/counter 用 Retriever 真构造（防查询串漂移），
    embedder / 集合探针为录制型桩."""
    from knowledge.retriever import Retriever

    embedded: list[str] = []
    warmed: dict = {}

    def _warm_collections(probe, champion=None):
        warmed["probe"] = probe
        warmed["champion"] = champion

    retriever = SimpleNamespace(
        available=available,
        list_champions=lambda: champions,
        matchup_query=Retriever.matchup_query,
        counter_query=Retriever.counter_query,
        embedder=SimpleNamespace(embed_queries=lambda qs: embedded.extend(qs)),
        warm_collections=_warm_collections,
    )
    return retriever, embedded, warmed


def test_warmup_sync_covers_champions_events_and_probe():
    """预热应覆盖：全部英雄 matchup/counter + 固定事件模板 + 兜底探针串."""
    retriever, embedded, warmed = _fake_retriever(["Ahri", "Zed"])
    lifecycle._warmup_sync(retriever)

    assert "matchup against Ahri early game laning tips" in embedded
    assert "Ahri enemy tips counter" in embedded
    assert "matchup against Zed early game laning tips" in embedded
    # 事件模板：与 routing._build_rag_query 的静态片段逐字一致才能命中缓存
    assert "dragon fight positioning objective strategy" in embedded
    assert (
        "after getting a kill what to do objective push tower "
        "dragon capitalize advantage" in embedded
    )
    assert "strategy tips priority" in embedded  # 兜底 + 集合探针复用
    assert warmed == {"probe": "strategy tips priority", "champion": "Ahri"}


def test_warmup_sync_without_champions_still_warms_events():
    """KB 为空（或摄入中）→ 英雄查询缺席，但事件模板与探针照常预热."""
    retriever, embedded, warmed = _fake_retriever([])
    lifecycle._warmup_sync(retriever)

    assert not any("matchup against" in q for q in embedded)
    assert "strategy tips priority" in embedded
    assert warmed == {"probe": "strategy tips priority", "champion": None}


@pytest.mark.asyncio
async def test_bg_warmup_skips_when_rag_unavailable():
    """RAG 不可用 → 静默跳过（不打 API、不探集合）."""
    retriever, embedded, warmed = _fake_retriever([], available=False)
    await lifecycle.bg_warmup(SimpleNamespace(retriever=retriever), None)
    assert embedded == []
    assert warmed == {}


@pytest.mark.asyncio
async def test_bg_warmup_runs_after_ingest_completes():
    """摄入进行中 → 预热必须等它完成再执行（KB 半成品时不预热）."""
    import asyncio

    order: list[str] = []

    async def slow_ingest():
        await asyncio.sleep(0.05)
        order.append("ingest")

    ingest = asyncio.create_task(slow_ingest())
    retriever, embedded, warmed = _fake_retriever(["Ahri"])
    await lifecycle.bg_warmup(SimpleNamespace(retriever=retriever), ingest)

    assert order == ["ingest"]
    assert "matchup against Ahri early game laning tips" in embedded


@pytest.mark.asyncio
async def test_bg_warmup_aborts_when_ingest_fails():
    """摄入失败 → 预热静默放弃（bg_ingest 已记日志），不抛出."""
    import asyncio

    async def boom():
        raise RuntimeError("ingest died")

    ingest = asyncio.create_task(boom())
    retriever, embedded, warmed = _fake_retriever(["Ahri"])
    await lifecycle.bg_warmup(SimpleNamespace(retriever=retriever), ingest)
    assert embedded == []
    assert warmed == {}
