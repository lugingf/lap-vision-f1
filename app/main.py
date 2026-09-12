from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.deps import get_historical_service, get_prefetch_scheduler, get_settings
from app.routes.f1 import router as f1_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    historical_service = get_historical_service()
    prefetch_scheduler = get_prefetch_scheduler()
    settings.fastf1_cache_dir.mkdir(parents=True, exist_ok=True)
    settings.data_cache_dir.mkdir(parents=True, exist_ok=True)
    if settings.prefetch_enabled:
        prefetch_scheduler.start()
    yield
    prefetch_scheduler.stop()
    historical_service.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="Lap Vision F1",
    description="Internal Fast-F1 ingestion and cache service for Lap Vision.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(f1_router)
