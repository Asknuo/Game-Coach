"""Game Coach Agent — FastAPI 入口（组件装配 + 生命周期 + 路由注册）."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from context import build_context
from planner.planner import SKILL_REGISTRY
from routers import http_router, ws_router
from services import lifecycle

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

ctx = build_context()


@asynccontextmanager
async def lifespan(app: FastAPI):
    node_count = len(ctx.coaching_graph.nodes) if hasattr(ctx.coaching_graph, "nodes") else 0
    rag_status = "on" if ctx.retriever.available else "off"
    logger.info(
        "Game Coach Agent ready — skills=%d, nodes=%d, rag=%s",
        len(SKILL_REGISTRY),
        node_count,
        rag_status,
    )
    if not ctx.retriever.available:
        logger.warning("RAG unavailable — set LLM_API_KEY and run: python -m knowledge.ingest")

    # 保留后台任务引用，防止被 GC 提前回收（弱引用 create_task 的已知陷阱）
    ingest_task = lifecycle.maybe_start_ingest(ctx)
    save_task = asyncio.create_task(lifecycle.periodic_save(ctx))
    # 知识预热：等可能的摄入完成后，把游戏期的冷启动成本挪到启动时
    warmup_task = asyncio.create_task(lifecycle.bg_warmup(ctx, ingest_task))
    # 流式增量合并广播 worker：把 tip_stream 的网络 IO 与 LLM 迭代解耦
    ctx.stream_broadcaster.start()
    yield
    save_task.cancel()
    warmup_task.cancel()
    if ingest_task and not ingest_task.done():
        ingest_task.cancel()
    await ctx.stream_broadcaster.stop()
    # 取消只是请求停止：必须 await 到真正结束，否则 to_thread 里的
    # 写盘/预热线程仍在跑，且 cancel 未 await 的任务会挂 pending 警告
    await asyncio.gather(
        save_task, warmup_task, ingest_task,
        return_exceptions=True,
    )
    # 连接池显式关闭：Redis / AsyncOpenAI / Embedder 不 close 会在
    # uvicorn --reload 时每次重载漏一批连接（unclosed-session 刷屏）
    await ctx.redis_store.close()
    await ctx.llm.aclose()
    retriever = ctx.retriever
    close_embedder = getattr(retriever, "close", None)
    if close_embedder is not None:
        close_embedder()
    ctx.memory_store.save("default", ctx.memory)
    logger.info("Memory saved to disk")


app = FastAPI(title="Game Coach Agent", lifespan=lifespan)
app.state.ctx = ctx

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(http_router)
app.include_router(ws_router)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run("app:app", host=host, port=port, reload=True)
