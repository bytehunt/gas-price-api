"""Tiny on-disk JSON cache.

Point of this: geocoding the same ZIP on every run is rude to Nominatim, and
while developing you don't want to re-hit a price provider on each iteration.
Disabled by setting a TTL of 0 (which is what CI does for prices).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


def default_cache_dir() -> Path:
    env = os.environ.get("GASPRICE_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "gasprice"


class JsonCache:
    def __init__(self, root: Path | str | None = None, ttl: float = 600.0) -> None:
        self.root = Path(root) if root else default_cache_dir()
        self.ttl = float(ttl)

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.root / namespace / f"{digest}.json"

    def get(self, namespace: str, key: str) -> Any | None:
        if self.ttl <= 0:
            return None
        path = self._path(namespace, key)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - raw.get("_at", 0) > self.ttl:
            return None
        return raw.get("value")

    def set(self, namespace: str, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"_at": time.time(), "value": value}), "utf-8")
        tmp.replace(path)
