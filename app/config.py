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


def _f1tv_token_file(value: str | None) -> Path | None:
    if value:
        return Path(value).expanduser().resolve()
    try:
        from fastf1.internals.f1auth import AUTH_DATA_FILE

        return Path(AUTH_DATA_FILE)
    except Exception:  # noqa: BLE001 - FastF1's login file is a convenience, not a requirement
        return None


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
    f1tv_token_file: Path | None
    live_snapshot_enabled: bool
    live_replay_enabled: bool
    prefetch_enabled: bool
    prefetch_interval_seconds: int
    prefetch_lookback_hours: int
    prefetch_playback_detail_step_ms: int


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
        # Where the F1TV subscription token is read from. Left unset it is the file FastF1's own login
        # writes, so a machine that has logged in once is connected with the cars; on a server it is a
        # file put there, and a missing one means connecting without a subscription.
        f1tv_token_file=_f1tv_token_file(os.getenv("LAP_VISION_F1_F1TV_TOKEN_FILE")),
        live_snapshot_enabled=os.getenv("LAP_VISION_F1_LIVE_SNAPSHOT", "true").strip().lower()
        not in ("0", "false", ""),
        live_replay_enabled=os.getenv("LAP_VISION_F1_LIVE_REPLAY", "false").strip().lower() in ("1", "true"),
        prefetch_enabled=os.getenv("LAP_VISION_F1_PREFETCH_ENABLED", "true").strip().lower() not in ("0", "false", ""),
        prefetch_interval_seconds=max(60, int(os.getenv("LAP_VISION_F1_PREFETCH_INTERVAL_SECONDS", "300"))),
        prefetch_lookback_hours=max(1, int(os.getenv("LAP_VISION_F1_PREFETCH_LOOKBACK_HOURS", "96"))),
        # The finer playback the scrubbing view asks for. Warmed in the background because the
        # first caller otherwise pays for a full session load; 0 switches the warming off, for a
        # deployment that would rather have the disk than the wait.
        prefetch_playback_detail_step_ms=max(
            0, int(os.getenv("LAP_VISION_F1_PREFETCH_PLAYBACK_DETAIL_STEP_MS", "250"))
        ),
    )
