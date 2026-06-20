"""
Live Client Data API 采集器。

当对局进行中时，通过 https://127.0.0.1:2999 实时读取游戏状态。
Riot 官方 API，不需要任何第三方库，不需要认证。

API 文档:
  https://developer.riotgames.com/docs/lol#game-client-api_live-client-data-api

使用方式:
  collector = LiveClientCollector(callback=on_game_state)
  collector.start()  # 后台线程轮询
"""

import json
import logging
import ssl
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("collector.live_client")

LIVE_CLIENT_URL = "https://127.0.0.1:2999"
POLL_INTERVAL = 1.0  # 秒


# ── 数据结构（对应 Live Client Data API 返回格式） ──


@dataclass
class LivePlayer:
    summoner_name: str = ""
    team: str = ""
    level: int = 0
    champion_name: str = ""
    current_gold: float = 0
    health: float = 0
    max_health: float = 0
    mana: float = 0
    max_mana: float = 0
    deaths: int = 0
    kills: int = 0
    assists: int = 0
    creep_score: int = 0
    items: list[dict] = field(default_factory=list)
    runes: dict = field(default_factory=dict)
    summoner_spells: list[str] = field(default_factory=list)
    position_x: float = 0
    position_y: float = 0
    is_dead: bool = False


@dataclass
class LiveGameData:
    game_time: float = 0
    map_name: str = ""
    game_mode: str = ""
    active_player: Optional[LivePlayer] = None
    all_players: list[LivePlayer] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    @property
    def active_team(self) -> str:
        if self.active_player:
            return self.active_player.team
        return ""

    def get_enemy_laner(self) -> Optional[LivePlayer]:
        """根据位置找对线敌人。"""
        if not self.active_player:
            return None
        active_role = _guess_role(self.active_player)
        for p in self.all_players:
            if p.team != self.active_player.team and _guess_role(p) == active_role:
                return p
        return None

    def active_health_pct(self) -> float:
        ap = self.active_player
        if not ap or ap.max_health <= 0:
            return 100.0
        return ap.health / ap.max_health * 100


def _guess_role(player: LivePlayer) -> str:
    """根据召唤师技能推断位置。"""
    spells = set(s.lower() for s in player.summoner_spells)
    if "smite" in spells:
        return "jungle"
    # 简化：用召唤师技能组合猜
    return "lane"


class LiveClientCollector:
    """轮询 Live Client Data API，解析为结构化数据，回调通知上层。"""

    def __init__(
        self,
        callback: Callable[[str, dict], None],
        poll_interval: float = POLL_INTERVAL,
    ):
        self._callback = callback  # (event_type, payload) -> None
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False

        # 上次数据快照，用于检测变化
        self._last_game_data: Optional[LiveGameData] = None
        self._last_events: list[dict] = []
        self._last_hp: float = 100.0
        self._last_deaths: int = 0
        self._last_items_snapshot: dict = {}  # {"by_slot": {slot: itemID}, "ids": [itemID, ...]}
        self._last_gold: float = 0
        self._dragon_kills: dict[str, int] = {}  # team -> kills
        self._baron_kills: dict[str, int] = {}
        self._kills_snapshot: dict[str, int] = {}  # summoner_name -> kills
        self._enemy_items_snapshot: dict[str, list[int]] = {}  # enemy_name -> [itemID, ...]
        self._last_event_time: float = 0  # 上次发送事件的 epoch 时间
        self._heartbeat_interval: float = 30.0  # 心跳间隔（秒）

    # ── 公开 API ──

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Live Client Collector started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._connected = False
        logger.info("Live Client Collector stopped")

    # ── 轮询循环 ──

    def _poll_loop(self):
        while self._running:
            try:
                data = self._fetch("/liveclientdata/allgamedata")
                if data:
                    if not self._connected:
                        self._connected = True
                        self._callback("live_client_connected", {})
                        logger.info("Live Client Data API connected")
                    game = self._parse(data)
                    self._detect_events(game)

                    # ★ 心跳机制：超过心跳间隔无事件，发送轻量 state
                    if time.time() - self._last_event_time > self._heartbeat_interval:
                        if self._last_event_time > 0:  # 首次连接不发心跳
                            state = self._build_game_state(game)
                            self._callback("state", state)
                            self._last_event_time = time.time()
                else:
                    if self._connected:
                        self._connected = False
                        self._last_game_data = None
                        self._last_events = []
                        self._callback("live_client_disconnected", {})
                        logger.info("Live Client Data API disconnected")
            except Exception as e:
                if self._connected:
                    self._connected = False
                    self._callback("live_client_disconnected", {})
                logger.debug("Live Client API unavailable: %s", e)

            time.sleep(self._poll_interval)

    # ── 事件检测 ──

    def _detect_events(self, game: LiveGameData):
        """对比上次快照，检测新事件并回调（只在有事件时附带 state）。"""
        ap = game.active_player
        if not ap:
            return

        state = self._build_game_state(game)
        state_sent = False  # 只在首个事件前发送一次 state

        def _emit(event_name: str, event_data: dict):
            """仅在首次事件前附带 state，后续事件不重复发送."""
            nonlocal state_sent
            if not state_sent:
                self._callback("state", state)
                state_sent = True
            self._callback("event", {"name": event_name, "data": event_data})
            self._last_event_time = time.time()  # ★ 记录最后事件时间，用于心跳判定

        # 2. 低血量
        hp_pct = game.active_health_pct()
        if hp_pct < 30 and self._last_hp >= 30:
            enemy = game.get_enemy_laner()
            _emit("low_health", {
                "health_pct": round(hp_pct, 1),
                "enemy_name": enemy.summoner_name if enemy else "",
                "enemy_champion": enemy.champion_name if enemy else "",
            })
        self._last_hp = hp_pct

        # 3. 死亡
        if ap.deaths > self._last_deaths:
            db = ap.deaths - self._last_deaths
            for _ in range(db):
                _emit("death", {
                    "total_deaths": ap.deaths,
                    "game_time": game.game_time,
                })
        self._last_deaths = ap.deaths

        # 4. 购买/合成/卖出装备 — 逐 slot 对比
        current_items_by_slot: dict[int, int] = {}
        current_item_ids: list[int] = []
        for it in ap.items:
            iid = it.get("itemID", 0)
            slot = it.get("slot", 0)
            if iid != 0:
                current_items_by_slot[slot] = iid
                current_item_ids.append(iid)

        prev_items_by_slot = self._last_items_snapshot.get("by_slot", {})
        prev_ids = set(self._last_items_snapshot.get("ids", []))

        new_items = [iid for iid in current_item_ids if iid not in prev_ids]
        removed_items = [iid for iid in prev_ids if iid not in current_item_ids]
        upgraded_slots: dict[int, tuple[int, int]] = {}  # slot -> (old_id, new_id)

        for slot, new_id in current_items_by_slot.items():
            old_id = prev_items_by_slot.get(slot, 0)
            if old_id != 0 and old_id != new_id and new_id != 0:
                upgraded_slots[slot] = (old_id, new_id)

        if new_items:
            # 过滤掉只是在合成中替换了（已算作 upgrade）
            pure_new = [iid for iid in new_items if iid not in [t[1] for t in upgraded_slots.values()]]
            for iid in pure_new:
                _emit("item_purchased", {
                    "item_id": iid,
                    "slot": _slot_for_item(current_items_by_slot, iid),
                    "action": "purchased",
                })

        if removed_items:
            # 过滤掉合成替换的旧散件（已算作 upgrade）
            pure_removed = [iid for iid in removed_items if iid not in [t[0] for t in upgraded_slots.values()]]
            for iid in pure_removed:
                _emit("item_sold", {
                    "item_id": iid,
                    "action": "sold_or_consumed",
                })

        if upgraded_slots:
            for slot, (old_id, new_id) in upgraded_slots.items():
                _emit("item_upgraded", {
                    "slot": slot,
                    "old_item_id": old_id,
                    "new_item_id": new_id,
                    "action": "upgraded",
                })

        self._last_items_snapshot = {"by_slot": current_items_by_slot, "ids": current_item_ids}

        # 5. 金币变化（升级技能/大件后触发）
        if self._last_gold > 0:
            gold_delta = ap.current_gold - self._last_gold
            if gold_delta > 500:
                _emit("gold_spike", {
                    "current_gold": ap.current_gold,
                    "delta": round(gold_delta),
                })
        self._last_gold = ap.current_gold

        # 6. 击杀
        current_kills = {}
        for p in game.all_players:
            current_kills[p.summoner_name] = p.kills
        for name, kills in current_kills.items():
            prev_kills = self._kills_snapshot.get(name, 0)
            if kills > prev_kills and name == ap.summoner_name:
                _emit("kill", {
                    "killer": name,
                    "total_kills": kills,
                    "game_time": game.game_time,
                })
        self._kills_snapshot = current_kills

        # 7. 龙 / 大龙 — 从 events 里解析
        self._detect_objectives(game, _emit)

        # 8. 敌人装备变化 — 对比所有敌方玩家
        self._detect_enemy_items(game, _emit)

        self._last_game_data = game

    @staticmethod
    def _slot_for_item(items_by_slot: dict[int, int], item_id: int) -> int:
        """根据 itemID 查找所在 slot."""
        for slot, iid in items_by_slot.items():
            if iid == item_id:
                return slot
        return 0

    def _detect_objectives(self, game: LiveGameData, _emit):
        """检测龙/大龙击杀事件。"""
        events = game.events
        for evt in events:
            evt_name = evt.get("EventName", "")
            if evt_name == "DragonKill":
                team = self._dragon_team(evt, game)
                self._dragon_kills[team] = self._dragon_kills.get(team, 0) + 1
                # 计算下一条龙刷新时间 (5 分钟)
                respawn = evt.get("EventTime", game.game_time) + 300
                _emit("dragon_soon", {
                    "dragon_type": evt.get("DragonType", "unknown"),
                    "killer_team": team,
                    "respawn_time": respawn,
                    "stolen": evt.get("Stolen", False),
                })
            elif evt_name == "BaronKill":
                team = self._baron_team(evt, game)
                _emit("baron_soon", {
                    "killer_team": team,
                    "respawn_time": evt.get("EventTime", game.game_time) + 420,
                })

    def _detect_enemy_items(self, game: LiveGameData, _emit):
        """检测敌方所有英雄的装备变化."""
        ap = game.active_player
        if not ap:
            return

        active_team = game.active_team
        if not active_team:
            return

        for p in game.all_players:
            if p.team == active_team or p.team == "":
                continue  # 跳过自己和无效队伍的

            # 当前装备列表（只取有效 itemID）
            current_items = sorted([
                it.get("itemID", 0) for it in p.items if it.get("itemID", 0) != 0
            ])
            prev_items = sorted(self._enemy_items_snapshot.get(p.summoner_name, []))

            # 找出新增的装备
            new_items = [iid for iid in current_items if iid not in prev_items]
            if new_items:
                _emit("enemy_item_purchased", {
                    "enemy_name": p.summoner_name,
                    "enemy_champion": p.champion_name,
                    "item_ids": new_items,
                    "total_items": len(current_items),
                    "game_time": game.game_time,
                })

            # 更新快照
            self._enemy_items_snapshot[p.summoner_name] = current_items

    def _dragon_team(self, evt: dict, game: LiveGameData) -> str:
        """根据击杀者的 summoner_name 找到所属队伍."""
        killer_name = evt.get("KillerName", "")
        for p in game.all_players:
            if p.summoner_name == killer_name:
                return p.team
        return "unknown"

    def _baron_team(self, evt: dict, game: LiveGameData) -> str:
        killer_name = evt.get("KillerName", "")
        for p in game.all_players:
            if p.summoner_name == killer_name:
                return p.team
        return "unknown"

    # ── HTTP 请求 ──

    def _fetch(self, path: str) -> Optional[dict]:
        """向 Live Client Data API 发 GET 请求（忽略自签名证书）."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = LIVE_CLIENT_URL + path
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            return None

    # ── 数据解析 ──

    def _parse(self, raw: dict) -> LiveGameData:
        """将 Live Client API 原始 JSON 解析为 LiveGameData."""
        game = LiveGameData()
        game.game_time = raw.get("gameData", {}).get("gameTime", 0)
        game.game_mode = raw.get("gameData", {}).get("gameMode", "")
        game.map_name = raw.get("gameData", {}).get("mapName", "")

        # 所有玩家
        all_raw = raw.get("allPlayers", [])
        for pr in all_raw:
            lp = self._parse_player(pr)
            game.all_players.append(lp)

        # 当前玩家
        ap_raw = raw.get("activePlayer", {})
        if ap_raw:
            ap_name = ap_raw.get("summonerName", "")
            ap_match = next((p for p in game.all_players if p.summoner_name == ap_name), None)
            if ap_match:
                game.active_player = ap_match
            else:
                game.active_player = self._parse_player(ap_raw)

        # 事件
        events_raw = raw.get("events", {}).get("Events", [])
        game.events = events_raw

        return game

    def _parse_player(self, raw: dict) -> LivePlayer:
        """解析单个玩家数据."""
        p = LivePlayer()
        p.summoner_name = raw.get("summonerName", "")
        p.team = raw.get("team", "")
        p.level = raw.get("level", 0)
        p.champion_name = raw.get("championName", "")
        p.current_gold = raw.get("currentGold", 0)
        stats = raw.get("championStats", {}) or {}
        p.health = stats.get("currentHealth", 0)
        p.max_health = stats.get("maxHealth", 1)
        p.mana = stats.get("resourceValue", 0)
        p.max_mana = stats.get("resourceMax", 1)
        scores = raw.get("scores", {}) or {}
        p.deaths = scores.get("deaths", 0)
        p.kills = scores.get("kills", 0)
        p.assists = scores.get("assists", 0)
        p.creep_score = scores.get("creepScore", 0)
        p.items = raw.get("items", []) or []
        p.runes = raw.get("runes", {}) or {}
        spells = raw.get("summonerSpells", {}) or {}
        s1 = (spells.get("summonerSpellOne") or {}).get("rawDisplayName", "")
        s2 = (spells.get("summonerSpellTwo") or {}).get("rawDisplayName", "")
        p.summoner_spells = [s1, s2]
        pos = raw.get("position", {}) or {}
        p.position_x = pos.get("x", 0)
        p.position_y = pos.get("y", 0)
        p.is_dead = (raw.get("championStats") or {}).get("currentHealth", 1) <= 0
        return p

    # ── 构建 Agent 期望的 GameState ──

    def _build_game_state(self, game: LiveGameData) -> dict:
        """转换为 Agent models/state.py 中 GameState 格式."""
        ap = game.active_player
        return {
            "game_time": game.game_time,
            "active_player": {
                "summoner_name": ap.summoner_name if ap else "",
                "level": ap.level if ap else 0,
                "current_gold": ap.current_gold if ap else 0,
                "health": ap.health if ap else 0,
                "max_health": ap.max_health if ap else 1,
                "position": {"x": ap.position_x if ap else 0, "y": ap.position_y if ap else 0},
            },
            "all_players": [
                {
                    "summoner_name": p.summoner_name,
                    "team": p.team,
                    "level": p.level,
                    "health": p.health,
                    "max_health": p.max_health,
                    "position": {"x": p.position_x, "y": p.position_y},
                    "items": [
                        {"itemID": it.get("itemID", 0), "slot": it.get("slot", 0)}
                        for it in p.items if it.get("itemID", 0) != 0
                    ],
                }
                for p in game.all_players
            ],
            "events": game.events,
        }