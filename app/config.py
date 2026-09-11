from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / ".env.local", override=True)


def _resolve_dir(value: str | None, default: Path) -> Path:
    if not value:
        return default
    return Path(value).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    worker_processes: int
    internal_token: str
    root_dir: Path
    fastf1_cache_dir: Path
    data_cache_dir: Path
    live_data_dir: Path
    live_quantum_seconds: int


def load_settings() -> Settings:
    root_dir = ROOT_DIR
    var_dir = root_dir / "var"

    return Settings(
        host=os.getenv("LAP_VISION_F1_HOST", "0.0.0.0"),
        port=int(os.getenv("LAP_VISION_F1_PORT", "8010")),
        worker_processes=max(1, int(os.getenv("LAP_VISION_F1_WORKERS", "2"))),
        internal_token=os.getenv("LAP_VISION_F1_INTERNAL_TOKEN", "dev-token"),
        root_dir=root_dir,
        fastf1_cache_dir=_resolve_dir(
            os.getenv("LAP_VISION_F1_FASTF1_CACHE_DIR"),
            var_dir / "fastf1-cache",
        ),
        data_cache_dir=_resolve_dir(
            os.getenv("LAP_VISION_F1_DATA_CACHE_DIR"),
            var_dir / "data-cache",
        ),
        live_data_dir=_resolve_dir(
            os.getenv("LAP_VISION_F1_LIVE_DATA_DIR"),
            var_dir / "live-timing",
        ),
        live_quantum_seconds=max(1, int(os.getenv("LAP_VISION_F1_LIVE_QUANTUM_SECONDS", "5"))),
    )
