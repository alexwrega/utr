"""Tiny JSON disk cache with a TTL. Keys are plain identifiers, never credentials."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 24 * 60 * 60

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


class DiskCache:
    def __init__(self, root: Path | None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.root = root.expanduser() if root else None
        self.ttl_seconds = ttl_seconds
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str) -> Path:
        assert self.root is not None
        return self.root / _SAFE_KEY.sub("_", namespace) / f"{_SAFE_KEY.sub('_', key)}.json"

    def get(self, namespace: str, key: str) -> Any | None:
        if not self.root:
            return None
        path = self._path(namespace, key)
        try:
            if time.time() - path.stat().st_mtime > self.ttl_seconds:
                return None
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.root:
            return
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(path)
