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
    yield
    save_task.cancel()
    if ingest_task and not ingest_task.done():
        ingest_task.cancel()
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
