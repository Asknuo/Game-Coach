"""从 Riot Data Dragon API 拉取 LOL 游戏数据并缓存到本地。

数据来源：
- Champions: https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json
- Champion detail: https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion/{name}.json
- Items: https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json
- Runes: https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/runesReforged.json
- Summoner Spells: https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/summoner.json

用法: python -m knowledge.data_fetcher
"""

import json
import logging
import os
from urllib.request import urlopen

logger = logging.getLogger(__name__)

LATEST_VERSION_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CDN_BASE = "https://ddragon.leagueoflegends.com/cdn/{version}"


class DataDragonFetcher:
    """从 Data Dragon 下载并缓存游戏数据。"""

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            data_dir = os.path.join(os.path.dirname(__file__), "data")
        self.data_dir = data_dir
        self._resolve_version()

    def _resolve_version(self):
        try:
            with urlopen(LATEST_VERSION_URL) as resp:
                versions = json.loads(resp.read().decode("utf-8"))
                self.version = versions[0]
                logger.info("Latest DDragon version: %s", self.version)
        except Exception:
            logger.warning("Failed to get latest version, using fallback 16.12.1")
            self.version = "16.12.1"

    def _fetch_json(self, url: str) -> dict | list | None:
        try:
            logger.info("Fetching %s", url)
            with urlopen(url) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            logger.exception("Failed to fetch %s", url)
            return None

    def _save_json(self, filename: str, data: dict | list) -> str:
        os.makedirs(self.data_dir, exist_ok=True)
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return filepath

    def fetch_champions(self) -> list[dict] | None:
        """拉取所有英雄数据（含技能详情），返回 list[champion_data]。

        先拉列表，再逐个拉取详情以获取 spells/passive。
        """
        url = f"{CDN_BASE}/data/en_US/champion.json".format(version=self.version)
        raw = self._fetch_json(url)
        if raw is None:
            return None

        data_obj = raw.get("data", raw) if isinstance(raw, dict) else {}
        champion_list = list(data_obj.values())
        champion_list.sort(key=lambda c: c.get("name", ""))

        # 逐个拉取详细数据（含技能）
        champions = []
        for ch in champion_list:
            champ_id = ch.get("id", "")
            detail_url = f"{CDN_BASE}/data/en_US/champion/{champ_id}.json".format(version=self.version)
            detail = self._fetch_json(detail_url)
            if detail:
                detail_data = detail.get("data", {}).get(champ_id, {})
                if detail_data:
                    champions.append(detail_data)
                    continue
            # 回退到列表数据（无技能）
            champions.append(ch)

        champions.sort(key=lambda c: c.get("name", ""))
        self._save_json("champions.json", champions)
        logger.info("Fetched %d champions (with skills)", len(champions))
        return champions

    def fetch_items(self) -> dict[str, dict] | None:
        """拉取所有装备数据，返回 {itemID: item_data} dict。"""
        url = f"{CDN_BASE}/data/en_US/item.json".format(version=self.version)
        raw = self._fetch_json(url)
        if raw is None:
            return None

        data_obj = raw.get("data", raw) if isinstance(raw, dict) else {}
        # 保留原始 ID 作为 key，同时把 ID 写入每个 item 内部方便后续使用
        items = {}
        for item_id, item_data in data_obj.items():
            item_data["id"] = item_id
            items[item_id] = item_data
        self._save_json("items.json", items)
        logger.info("Fetched %d items (with IDs)", len(items))
        return items

    def fetch_runes(self) -> list[dict] | None:
        """拉取所有符文数据。"""
        url = f"{CDN_BASE}/data/en_US/runesReforged.json".format(version=self.version)
        raw = self._fetch_json(url)
        if raw is None or not isinstance(raw, list):
            return None

        self._save_json("runes.json", raw)
        logger.info("Fetched %d rune paths", len(raw))
        return raw

    def fetch_summoner_spells(self) -> list[dict] | None:
        """拉取所有召唤师技能数据。"""
        url = f"{CDN_BASE}/data/en_US/summoner.json".format(version=self.version)
        raw = self._fetch_json(url)
        if raw is None:
            return None

        data_obj = raw.get("data", raw) if isinstance(raw, dict) else {}
        spells = list(data_obj.values())
        spells.sort(key=lambda s: s.get("name", ""))
        self._save_json("summoner_spells.json", spells)
        logger.info("Fetched %d summoner spells", len(spells))
        return spells

    def fetch_all(self) -> bool:
        """拉取所有数据并缓存到本地。"""
        success = True
        if not self.fetch_champions():
            success = False
        if not self.fetch_items():
            success = False
        if not self.fetch_runes():
            success = False
        if not self.fetch_summoner_spells():
            success = False
        return success


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fetcher = DataDragonFetcher()
    fetcher.fetch_all()
    logger.info("Data fetch complete")
