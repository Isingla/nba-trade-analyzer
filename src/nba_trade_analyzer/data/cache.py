"""Generic JSON file cache with TTL.

Stores values under a configurable directory as `<key>.json`. Each entry
records an expiry timestamp; reads after expiry behave as a cache miss.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path.home() / ".nba_trade_analyzer" / "cache"


class JsonCache:
    def __init__(self, cache_dir: Path | str | None = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Restrict to a flat namespace so callers can't escape the cache dir.
        safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in key)
        return self.cache_dir / f"{safe}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() > payload.get("expires_at", 0):
            return None
        return payload.get("value")

    def set(self, key: str, value: Any, ttl_hours: float) -> None:
        payload = {
            "expires_at": time.time() + ttl_hours * 3600,
            "value": value,
        }
        self._path(key).write_text(json.dumps(payload), encoding="utf-8")
