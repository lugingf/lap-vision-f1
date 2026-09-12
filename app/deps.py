from __future__ import annotations

from app.cache import CacheManager
from app.config import Settings, load_settings
from app.services.historical import HistoricalService
from app.services.live import LiveSessionService
from app.services.prefetch import SessionPrefetchScheduler

_settings = load_settings()
_cache = CacheManager(_settings.data_cache_dir)
_historical_service = HistoricalService(
    fastf1_cache_dir=_settings.fastf1_cache_dir,
    cache=_cache,
    worker_processes=_settings.worker_processes,
)
_live_service = LiveSessionService(
    historical=_historical_service,
    live_data_dir=_settings.live_data_dir,
    quantum_seconds=_settings.live_quantum_seconds,
)
_prefetch_scheduler = SessionPrefetchScheduler(
    historical=_historical_service,
    cache=_cache,
    interval_seconds=_settings.prefetch_interval_seconds,
    lookback_hours=_settings.prefetch_lookback_hours,
    live=_live_service,
)


def get_settings() -> Settings:
    return _settings


def get_historical_service() -> HistoricalService:
    return _historical_service


def get_live_service() -> LiveSessionService:
    return _live_service


def get_prefetch_scheduler() -> SessionPrefetchScheduler:
    return _prefetch_scheduler
