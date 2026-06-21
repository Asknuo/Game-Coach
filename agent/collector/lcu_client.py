"""
LCU API 采集器。

通过 League Client 的本地 HTTPS API 读取客户端数据：
- 当前召唤师信息（昵称、等级、图标）
- 英雄熟练度（所有英雄的 mastery level + points）
- 符文页（当前/所有）
- GameFlow 状态（大厅/选英雄/加载中/游戏中/结算）
- Champion Select 数据（选的谁、ban 的谁、队友对手）

API 文档:
  https://developer.riotgames.com/docs/lol#game-client-api_league-client-api

启动方式:
  collector = LCUClientCollector(callback=on_lcu_data)
  collector.start()  # 后台线程轮询

注意:
  - 需要在 League Client 打开时才能工作
  - lockfile 位于 Riot Games 安装目录下
  - 每次 Client 启动端口和密码都会变
"""

import base64
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("collector.lcu_client")

# lockfile 常见路径
_LOCKFILE_PATHS = [
    r"D:\WeGameApps\英雄联盟\Riot Client Data\User Data\Config\lockfile",  # WeGame 国服（正确位置）
    os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\Config\lockfile"),
    os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\League of Legends\lockfile"),
    r"C:\Riot Games\League of Legends\lockfile",
    r"C:\Riot Games\Riot Client\lockfile",
]

POLL_INTERVAL = 2.0  # LCU 不需要太频繁


@dataclass
class SummonerInfo:
    summoner_id: int = 0
    account_id: int = 0
    display_name: str = ""
    summoner_level: int = 0
    profile_icon_id: int = 0
    puuid: str = ""


@dataclass
class ChampionMastery:
    champion_id: int = 0
    champion_level: int = 0  # 1-7
    champion_points: int = 0
    last_play_time: int = 0
    chest_granted: bool = False


@dataclass
class GameFlowState:
    phase: str = ""  # None/Lobby/Matchmaking/ReadyCheck/ChampSelect/InProgress/EndOfGame
    game_id: int = 0
    queue_id: int = 0


@dataclass
class ChampSelectInfo:
    local_player_cell_id: int = 0
    actions: list[dict] = field(default_factory=list)  # pick/ban actions
    my_team: list[dict] = field(default_factory=list)
    their_team: list[dict] = field(default_factory=list)
    timer: float = 0  # phase timer seconds remaining
    phase: str = ""  # BAN_PICK / FINALIZATION


@dataclass
class RunePage:
    id: int = 0
    name: str = ""
    primary_style_id: int = 0
    sub_style_id: int = 0
    perk_ids: list[int] = field(default_factory=list)
    is_active: bool = False


@dataclass
class LCUData:
    summoner: Optional[SummonerInfo] = None
    masteries: list[ChampionMastery] = field(default_factory=list)
    gameflow: Optional[GameFlowState] = None
    champ_select: Optional[ChampSelectInfo] = None
    current_runes: Optional[RunePage] = None
    all_rune_pages: list[RunePage] = field(default_factory=list)


class LCUClientCollector:
    """轮询 LCU API，获取客户端数据，回调通知上层。"""

    def __init__(
        self,
        callback: Callable[[str, dict], None],
        poll_interval: float = POLL_INTERVAL,
    ):
        self._callback = callback
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False

        self._port: int = 0
        self._password: str = ""
        self._base_url: str = ""
        self._auth_header: str = ""

        # 上次快照，检测变化
        self._last_phase: str = ""
        self._last_champ_select_phase: str = ""
        self._my_pick_done: bool = False

    # ── 公开 API ──

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def lcu_data(self) -> Optional[LCUData]:
        return self._latest_data

    def start(self):
        if self._running:
            return
        self._running = True
        self._latest_data = LCUData()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("LCU Client Collector started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._connected = False
        logger.info("LCU Client Collector stopped")

    # ── 轮询循环 ──

    def _poll_loop(self):
        while self._running:
            try:
                if not self._connected:
                    self._try_connect()
                if self._connected:
                    self._poll_data()
                else:
                    time.sleep(3)  # 没连上时慢一点
                    continue
            except Exception:
                logger.debug("LCU poll error", exc_info=True)
                self._connected = False
            time.sleep(self._poll_interval)

    def _try_connect(self):
        """尝试找到 lockfile 或读取进程命令行来连接 LCU API."""
        # 方法 1：尝试 lockfile
        lockfile_path = None
        for p in _LOCKFILE_PATHS:
            if os.path.exists(p):
                lockfile_path = p
                break

        if lockfile_path:
            if self._try_connect_with_lockfile(lockfile_path):
                return

        # 方法 2：从 LeagueClientUx.exe 进程命令行提取凭据（国服/新版客户端）
        if self._try_connect_from_process():
            return

    def _try_connect_with_lockfile(self, lockfile_path: str) -> bool:
        """通过 lockfile 连接 LCU API."""
        try:
            with open(lockfile_path, "r") as f:
                content = f.read().strip()
            logger.info("Lockfile found: %s (size=%d)", lockfile_path, len(content))
            parts = content.split(":")
            if len(parts) < 4:
                logger.debug("Lockfile format invalid: %d parts", len(parts))
                return False
            port = int(parts[2])
            password = parts[3]
            if not self._connect_lcu(port, password):
                return False
            logger.info("LCU API connected via lockfile (port=%d)", port)
            return True
        except Exception as e:
            logger.debug("Lockfile parse failed: %s", e)
            return False

    def _try_connect_from_process(self) -> bool:
        """通过 psutil 读取 LeagueClientUx.exe 命令行提取 LCU 凭据."""
        try:
            import psutil
        except ImportError:
            logger.debug("psutil not installed, skipping process-based LCU discovery")
            return False

        try:
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    info = proc.info
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if info["name"] != "LeagueClientUx.exe":
                    continue
                cmdline = info["cmdline"] or []
                # Parse --app-port and --remoting-auth-token
                port = None
                token = None
                for arg in cmdline:
                    m = re.match(r"--app-port=(\d+)", arg)
                    if m:
                        port = int(m.group(1))
                    m = re.match(r"--remoting-auth-token=(\S+)", arg)
                    if m:
                        token = m.group(1)
                if port and token:
                    logger.info("LCU discovered from LeagueClientUx.exe: port=%d", port)
                    if self._connect_lcu(port, token):
                        logger.info("LCU API connected via process (port=%d)", port)
                        return True
                    return False
        except Exception as e:
            logger.debug("Process-based LCU discovery failed: %s", e)
        return False

    def _connect_lcu(self, port: int, password: str) -> bool:
        """建立 LCU API 连接."""
        self._port = port
        self._password = password
        self._base_url = f"https://127.0.0.1:{port}"
        auth_raw = f"riot:{password}"
        self._auth_header = "Basic " + base64.b64encode(auth_raw.encode()).decode()

        try:
            resp = self._get("/lol-summoner/v1/current-summoner")
            if resp:
                self._connected = True
                self._callback("lcu_connected", {"summoner": resp})
                return True
            else:
                logger.debug("LCU API test: empty response")
        except Exception as e:
            logger.debug("LCU API test failed: %s", e)
        return False

    def _poll_data(self):
        """轮询各 LCU 端点，检测变化并回调."""
        # 1. 召唤师信息
        summoner = self._get("/lol-summoner/v1/current-summoner")
        if summoner:
            info = SummonerInfo(
                summoner_id=summoner.get("summonerId", 0),
                account_id=summoner.get("accountId", 0),
                display_name=summoner.get("displayName", ""),
                summoner_level=summoner.get("summonerLevel", 0),
                profile_icon_id=summoner.get("profileIconId", 0),
                puuid=summoner.get("puuid", ""),
            )
            self._latest_data.summoner = info

        # 2. GameFlow — 最重要的，检测阶段变化
        session = self._get("/lol-gameflow/v1/session")
        if session:
            phase = session.get("phase", "")
            game_data = session.get("gameData", {}) or {}
            gameflow = GameFlowState(
                phase=phase,
                game_id=game_data.get("gameId", 0),
                queue_id=game_data.get("queue", {}).get("id", 0) if game_data.get("queue") else 0,
            )
            self._latest_data.gameflow = gameflow

            # 检测阶段变化
            if phase != self._last_phase:
                self._callback("gameflow_phase_change", {
                    "old_phase": self._last_phase,
                    "new_phase": phase,
                    "game_id": gameflow.game_id,
                })
                self._last_phase = phase

                # 进入英雄选择时获取符文
                if phase == "ChampSelect":
                    self._my_pick_done = False
                    self._fetch_runes()

                # 游戏开始时推送英雄 + 符文 + 熟练度
                if phase == "InProgress":
                    self._on_game_start()

        # 3. 英雄选择
        if self._last_phase == "ChampSelect":
            self._poll_champ_select()

    def _poll_champ_select(self):
        """轮询 Champion Select 状态."""
        cs = self._get("/lol-champ-select/v1/session")
        if not cs:
            return

        actions = cs.get("actions", []) or []
        my_team = cs.get("myTeam", []) or []
        their_team = cs.get("theirTeam", []) or []
        timer = cs.get("timer", {}).get("adjustedPositionInPhase", 0)
        local_id = cs.get("localPlayerCellId", 0)

        phase = ""
        for action_list in actions:
            for act in action_list:
                if act.get("actorCellId") == local_id:
                    action_type = act.get("type", "")
                    if action_type == "pick" and act.get("isInProgress"):
                        phase = "picking"
                    elif action_type == "ban" and act.get("isInProgress"):
                        phase = "banning"

        cs_info = ChampSelectInfo(
            local_player_cell_id=local_id,
            actions=actions,
            my_team=my_team,
            their_team=their_team,
            timer=timer,
            phase=phase,
        )
        self._latest_data.champ_select = cs_info

        if phase != self._last_champ_select_phase:
            self._last_champ_select_phase = phase
            if phase == "picking":
                self._callback("lcu_pick_phase", {
                    "phase": phase,
                    "timer": timer,
                })

        # 检测已选英雄
        if cs_info.my_team and not self._my_pick_done:
            my_cell = next((m for m in cs_info.my_team if m.get("cellId") == local_id), None)
            if my_cell and my_cell.get("championId", 0) > 0:
                self._my_pick_done = True
                self._callback("lcu_champion_picked", {
                    "champion_id": my_cell.get("championId"),
                    "champion_name": "",  # LCU 不直接给名字，需用 championId 查
                    "assigned_position": my_cell.get("assignedPosition", ""),
                    "spell1_id": my_cell.get("spell1Id", 0),
                    "spell2_id": my_cell.get("spell2Id", 0),
                })

    def _fetch_runes(self):
        """获取当前符文页."""
        page = self._get("/lol-perks/v1/currentpage")
        if page:
            rune = RunePage(
                id=page.get("id", 0),
                name=page.get("name", ""),
                primary_style_id=page.get("primaryStyleId", 0),
                sub_style_id=page.get("subStyleId", 0),
                perk_ids=page.get("selectedPerkIds", []) or [],
                is_active=page.get("isActive", False),
            )
            self._latest_data.current_runes = rune
            self._callback("lcu_runes_updated", {
                "primary_style_id": rune.primary_style_id,
                "sub_style_id": rune.sub_style_id,
                "perk_ids": rune.perk_ids,
            })

        # 英雄熟练度
        try:
            mastery_raw = self._get("/lol-champion-mastery/v1/local-player/champion-mastery")
            if mastery_raw and isinstance(mastery_raw, list):
                self._latest_data.masteries = [
                    ChampionMastery(
                        champion_id=m.get("championId", 0),
                        champion_level=m.get("championLevel", 0),
                        champion_points=m.get("championPoints", 0),
                        last_play_time=m.get("lastPlayTime", 0),
                        chest_granted=m.get("chestGranted", False),
                    )
                    for m in mastery_raw[:20]  # 只取前 20 个最熟练的
                ]
                self._callback("lcu_mastery_loaded", {
                    "count": len(self._latest_data.masteries),
                })
        except Exception:
            pass

    def _on_game_start(self):
        """游戏开始时推送完整上下文给 Agent."""
        summoner = self._latest_data.summoner
        runes = self._latest_data.current_runes
        data = {
            "summoner_name": summoner.display_name if summoner else "",
            "summoner_level": summoner.summoner_level if summoner else 0,
            "runes": {
                "primary_style_id": runes.primary_style_id if runes else 0,
                "sub_style_id": runes.sub_style_id if runes else 0,
                "perk_ids": runes.perk_ids if runes else [],
            } if runes else {},
            "top_masteries": [
                {"champion_id": m.champion_id, "level": m.champion_level, "points": m.champion_points}
                for m in self._latest_data.masteries
            ],
        }
        self._callback("lcu_game_start", data)
        logger.info("Game starting — context sent to agent")

    # ── HTTP 请求 ──

    def _get(self, path: str) -> Optional[dict]:
        """向 LCU API 发 GET 请求."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = self._base_url + path
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Authorization": self._auth_header,
        })
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionRefusedError, OSError, json.JSONDecodeError, Exception):
            return None
