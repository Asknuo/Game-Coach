import json
import os
from typing import Any

import redis


class RedisStore:
    """Short-term memory for the current game session."""

    def __init__(self, url: str | None = None):
        self.url = url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._client: redis.Redis | None = None
        self._memory: dict[str, Any] = {}

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self.url, decode_responses=True)
        return self._client

    def _key(self, session_id: str, suffix: str) -> str:
        return f"coach:{session_id}:{suffix}"

    def save_state(self, session_id: str, state: dict[str, Any]) -> None:
        try:
            self.client.set(self._key(session_id, "state"), json.dumps(state), ex=7200)
        except redis.RedisError:
            self._memory["state"] = state

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        try:
            raw = self.client.get(self._key(session_id, "state"))
            return json.loads(raw) if raw else None
        except (redis.RedisError, json.JSONDecodeError):
            return self._memory.get("state")

    def mark_tip_sent(self, session_id: str, skill: str) -> None:
        key = self._key(session_id, f"tip:{skill}")
        try:
            self.client.set(key, "1", ex=120)
        except redis.RedisError:
            self._memory[f"tip:{skill}"] = True

    def was_tip_recently_sent(self, session_id: str, skill: str) -> bool:
        key = self._key(session_id, f"tip:{skill}")
        try:
            return bool(self.client.exists(key))
        except redis.RedisError:
            return bool(self._memory.get(f"tip:{skill}"))
