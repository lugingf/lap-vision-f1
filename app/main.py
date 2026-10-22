from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.deps import get_f1tv_auth, get_historical_service, get_prefetch_scheduler, get_settings
from app.routes.f1 import router as f1_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    historical_service = get_historical_service()
    prefetch_scheduler = get_prefetch_scheduler()
    settings.fastf1_cache_dir.mkdir(parents=True, exist_ok=True)
    settings.data_cache_dir.mkdir(parents=True, exist_ok=True)
    f1tv_auth = get_f1tv_auth()
    f1tv_auth.tokens.install()
    f1tv_renewal = asyncio.create_task(f1tv_auth.run())
    if settings.prefetch_enabled:
        prefetch_scheduler.start()
    yield
    prefetch_scheduler.stop()
    f1tv_renewal.cancel()
    historical_service.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="Lap Vision F1",
    description="Internal Fast-F1 ingestion and cache service for Lap Vision.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(f1_router)
