"""知识检索节点 — retrieve_knowledge（ChromaDB 多源聚合 RAG）."""

import asyncio
import logging

from graph.state import CoachState

logger = logging.getLogger(__name__)


def _find_nearest_enemy_champion(
    all_players: list[dict], active_team: str, active_x: float, active_y: float,
) -> str:
    """返回距离活跃玩家最近（<5000）的敌方英雄名，无则空串."""
    from models.state import is_position_valid

    best_dist = float("inf")
    nearest = ""
    for p in all_players:
        if not p.get("team") or p["team"] == active_team:
            continue
        enemy_pos = p.get("position", {})
        # 坐标有效性校验：排除 (0,0) 等无效坐标
        if not isinstance(enemy_pos, dict) or not is_position_valid(enemy_pos):
            continue
        ex = enemy_pos.get("x", 0)
        ey = enemy_pos.get("y", 0)
        dist = ((active_x - ex) ** 2 + (active_y - ey) ** 2) ** 0.5
        if dist < best_dist and dist < 5000:
            best_dist = dist
            nearest = p.get("champion_name", "")
    return nearest


def _resolve_champions(
    gs: dict, event_name: str, enemy_champion: str,
) -> tuple[str, str, float]:
    """从 game_state 解析 (己方英雄名, 敌方英雄名, 游戏时间).

    RAG 检索必须用英雄名（如 "Ahri"），不能用召唤师名；
    active_player.champion_name 由 sync_active_player 补全.
    """
    active_player = gs.get("active_player", {})
    summoner = active_player.get("summoner_name", "")
    champion = active_player.get("champion_name", "")

    all_players: list[dict] = gs.get("all_players", [])
    active_team = ""
    active_pos = active_player.get("position", {})
    for p in all_players:
        if p.get("summoner_name") == summoner:
            active_team = p.get("team", "")
            active_pos = p.get("position", active_pos)
            if not champion:
                champion = p.get("champion_name", "")
            break

    # 就近取敌方英雄用于克制攻略（enemy_item_purchased 已有明确敌方，跳过）
    if active_team and active_pos and event_name != "enemy_item_purchased":
        active_x = active_pos.get("x", 0) if isinstance(active_pos, dict) else 0
        active_y = active_pos.get("y", 0) if isinstance(active_pos, dict) else 0
        nearest = _find_nearest_enemy_champion(
            all_players, active_team, active_x, active_y,
        )
        if nearest:
            enemy_champion = nearest

    return champion, enemy_champion, gs.get("game_time", 0)


class RetrievalMixin:
    """向量检索节点（依赖 retriever，缺失时跳过）."""

    async def retrieve_knowledge(self, state: CoachState) -> CoachState:
        """ChromaDB RAG 检索 — 聚合己方+敌方英雄攻略、游戏机制等多源知识."""
        retriever = self.deps.retriever
        if not retriever:
            return state

        gs = state.get("game_state", {})
        event_name = state.get("event_name", "")

        # enemy events: 直接用事件数据中的敌方英雄（Go 侧填的是 ChampionName）
        enemy_champion = ""
        if event_name in ("enemy_item_purchased", "enemy_gold_lead", "enemy_fed"):
            enemy_champion = state.get("event_data", {}).get("enemy_champion", "")

        champion = ""
        game_time = 0.0
        if gs:
            champion, enemy_champion, game_time = _resolve_champions(
                gs, event_name, enemy_champion,
            )

        rag_query = state.get("rag_query", "")

        # embedding + ChromaDB 查询是同步 IO，offload 到线程池避免阻塞事件循环
        aggregated = await asyncio.to_thread(
            retriever.aggregate_coaching_context,
            ally_champion=champion,
            enemy_champion=enemy_champion if enemy_champion != champion else None,
            game_time=game_time,
            event_name=event_name,
            event_query=rag_query,
        )

        state["rag_docs"] = [aggregated] if aggregated else []
        logger.debug("retrieve: %d aggregated docs for %s", len(state["rag_docs"]), event_name)
        return state
