"""文件持久化存储 — 对标 DeerFlow 的 FileMemoryStorage."""

import glob
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from memory.models import PlayerMemory

logger = logging.getLogger(__name__)

MAX_BACKUPS = 5  # ★ HA #2: 保留最近 5 份备份


class MemoryStore:
    """Abstract base — 文件实现，后续可替换为 SQLite / Redis."""

    def __init__(self, base_dir: str | None = None):
        if base_dir is None:
            base_dir = os.path.join(os.path.dirname(__file__), "data")
        self.base_dir = base_dir
        self.backup_dir = os.path.join(base_dir, "backups")
        try:
            os.makedirs(base_dir, exist_ok=True)
            os.makedirs(self.backup_dir, exist_ok=True)
            self.available = True
        except OSError:
            logger.exception("memory data dir creation failed")
            self.available = False

    def _path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"{session_id}.json")

    def load(self, session_id: str) -> PlayerMemory | None:
        if not self.available:
            return None
        path = self._path(session_id)
        if not os.path.exists(path):
            return None
        try:
            return PlayerMemory.model_validate_json(Path(path).read_text(encoding="utf-8"))
        except Exception:
            logger.exception("load memory failed for %s", session_id)
            return None

    def save(self, session_id: str, memory: PlayerMemory):
        if not self.available:
            return
        memory.last_updated = datetime.now(timezone.utc).isoformat()

        # ★ HA #2: 保存前先备份当前文件（失败不阻塞写入）
        self._backup_before_save(session_id)

        try:
            Path(self._path(session_id)).write_text(
                memory.model_dump_json(indent=2), encoding="utf-8"
            )
        except Exception:
            logger.exception("save memory failed for %s", session_id)

    # ── HA #2: 备份轮转 ──────────────────────────────

    def _backup_before_save(self, session_id: str):
        """保存前先把当前文件复制到 backups/ 目录，保留最近 MAX_BACKUPS 份."""
        src = self._path(session_id)
        if not os.path.exists(src):
            return  # 首次写入，没有旧文件可备份
        try:
            ts = time.strftime("%Y%m%dT%H%M%S", time.localtime())
            dst = os.path.join(self.backup_dir, f"{session_id}.{ts}.json.bak")
            shutil.copy2(src, dst)
        except Exception:
            logger.exception("backup before save failed (save continues)")
            return

        # 清理旧备份：只保留最近 MAX_BACKUPS 份
        try:
            pattern = os.path.join(self.backup_dir, f"{session_id}.*.json.bak")
            files = sorted(glob.glob(pattern))
            if len(files) > MAX_BACKUPS:
                for old in files[:-MAX_BACKUPS]:
                    os.remove(old)
                    logger.debug("Removed old backup: %s", os.path.basename(old))
        except Exception:
            logger.exception("backup rotation cleanup failed")
