"""HTTP 查询端点 — /health /tips/latest /games."""

from __future__ import annotations

from fastapi import APIRouter, Request

from context import AppContext

router = APIRouter()


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


@router.get("/health")
async def health(request: Request):
    ctx = _ctx(request)
    memory = ctx.memory
    breaker = ctx.llm._breaker
    return {
        "status": "ok",
        "service": "game-coach-agent",
        "framework": "langgraph",
        "memory": {
            "facts": len(memory.facts),
            "games": len(memory.history.recent_games),
        },
        "runtime": {
            "overlay_clients": len(ctx.overlay_clients),
            "queue_depth": ctx.queue.pending_count,
            "llm_circuit_open": breaker.is_open(),
            "llm_failures": breaker._failures,
        },
        "metrics": ctx.metrics,
    }


@router.get("/tips/latest")
async def latest_tips(request: Request):
    ctx = _ctx(request)
    memory = ctx.memory
    state = await ctx.redis_store.get_state("default")
    return {
        "state": state,
        "memory": {
            "champion": memory.user.current_champion,
            "phase": memory.user.game_phase,
            "top_of_mind": memory.user.top_of_mind,
            "facts_count": len(memory.facts),
            "games_count": len(memory.history.recent_games),
        },
    }


@router.get("/games")
async def list_games(request: Request):
    """查询所有历史对局记录（永久保存，不限制数量）."""
    memory = _ctx(request).memory
    all_games = [g.model_dump() for g in memory.history.recent_games]
    return {
        "total": len(all_games),
        "games": all_games,
        "facts": [f.model_dump() for f in memory.facts],
    }
