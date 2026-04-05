from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CacheManager:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def read_json(self, relative_path: str) -> dict[str, Any] | None:
        path = self.root_dir / relative_path
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> None:
        path = self.root_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"), indent=2)
