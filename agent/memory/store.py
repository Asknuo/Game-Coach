"""文件持久化存储 — 对标 DeerFlow 的 FileMemoryStorage."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from memory.models import PlayerMemory

logger = logging.getLogger(__name__)


class MemoryStore:
    """Abstract base — 文件实现，后续可替换为 SQLite / Redis."""

    def __init__(self, base_dir: str | None = None):
        if base_dir is None:
            base_dir = os.path.join(os.path.dirname(__file__), "data")
        self.base_dir = base_dir
        try:
            os.makedirs(base_dir, exist_ok=True)
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
        try:
            Path(self._path(session_id)).write_text(
                memory.model_dump_json(indent=2), encoding="utf-8"
            )
        except Exception:
            logger.exception("save memory failed for %s", session_id)
