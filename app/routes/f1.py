from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.config import Settings
from app.deps import get_historical_service, get_live_service, get_settings
from app.domain.models import (
    HealthResponse,
    LiveSessionRequest,
    LiveSessionStatus,
    ProxySettingsRequest,
    ProxySettingsResponse,
    RacePlaybackRequest,
    RacePlaybackResponse,
    ScheduleResponse,
    ServiceOverview,
    SessionBundle,
    SessionRequest,
    TelemetryCompareRequest,
    TelemetryCompareResponse,
)
from app.services.historical import HistoricalService
from app.services.live import LiveSessionService

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]
HistoricalServiceDep = Annotated[HistoricalService, Depends(get_historical_service)]
LiveServiceDep = Annotated[LiveSessionService, Depends(get_live_service)]


def require_internal_token(
    settings: SettingsDep,
    token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    if not settings.internal_token:
        return
    if token == settings.internal_token:
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal token")


@router.get("/healthz", response_model=HealthResponse)
async def healthz(
    settings: SettingsDep,
) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="lap-vision-f1",
        fastf1_cache_dir=str(settings.fastf1_cache_dir),
        data_cache_dir=str(settings.data_cache_dir),
        worker_processes=settings.worker_processes,
    )


@router.get("/v1/overview", response_model=ServiceOverview, dependencies=[Depends(require_internal_token)])
async def overview() -> ServiceOverview:
    return ServiceOverview(
        service="lap-vision-f1",
        mode="historical-first",
        transport="http-polling-ready",
        capabilities=[
            "season-schedule",
            "historical-session-bundle",
            "fastf1-disk-cache",
            "normalized-json-cache",
            "process-pool-heavy-loads",
        ],
        cache_layers=[
            "fastf1-disk-cache",
            "normalized-json-cache",
        ],
        notes=[
            "Designed as an internal service for the Go backend, not as a public product API.",
            "Heavy Fast-F1 session loads are delegated to a process pool to avoid blocking the ASGI loop.",
            "Historical ingestion is the first delivery slice. Live transport will be added later.",
        ],
    )


@router.get(
    "/v1/seasons/{year}/schedule",
    response_model=ScheduleResponse,
    dependencies=[Depends(require_internal_token)],
)
async def season_schedule(
    year: int,
    historical_service: HistoricalServiceDep,
    refresh: bool = Query(default=False),
) -> ScheduleResponse:
    return await historical_service.get_schedule(year, refresh=refresh)


@router.post(
    "/v1/sessions/load",
    response_model=SessionBundle,
    dependencies=[Depends(require_internal_token)],
)
async def load_session(
    request: SessionRequest,
    historical_service: HistoricalServiceDep,
) -> SessionBundle:
    return await historical_service.load_session(request)


@router.post(
    "/v1/telemetry/compare",
    response_model=TelemetryCompareResponse,
    dependencies=[Depends(require_internal_token)],
)
async def telemetry_compare(
    request: TelemetryCompareRequest,
    historical_service: HistoricalServiceDep,
) -> TelemetryCompareResponse:
    return await historical_service.telemetry_compare(request)


@router.post(
    "/v1/race-playback",
    response_model=RacePlaybackResponse,
    dependencies=[Depends(require_internal_token)],
)
async def race_playback(
    request: RacePlaybackRequest,
    historical_service: HistoricalServiceDep,
) -> RacePlaybackResponse:
    return await historical_service.race_playback(request)


@router.get(
    "/v1/admin/proxy",
    response_model=ProxySettingsResponse,
    dependencies=[Depends(require_internal_token)],
)
async def get_proxy(historical_service: HistoricalServiceDep) -> ProxySettingsResponse:
    return ProxySettingsResponse(https_proxies=historical_service.get_proxy_urls())


@router.put(
    "/v1/admin/proxy",
    response_model=ProxySettingsResponse,
    dependencies=[Depends(require_internal_token)],
)
async def set_proxy(
    request: ProxySettingsRequest,
    historical_service: HistoricalServiceDep,
) -> ProxySettingsResponse:
    historical_service.set_proxy_urls(request.https_proxies)
    return ProxySettingsResponse(https_proxies=historical_service.get_proxy_urls())


@router.post(
    "/v1/live/sessions/start",
    response_model=LiveSessionStatus,
    dependencies=[Depends(require_internal_token)],
)
async def start_live_session(
    request: LiveSessionRequest,
    live_service: LiveServiceDep,
) -> LiveSessionStatus:
    return await live_service.start(request)


@router.post(
    "/v1/live/sessions/stop",
    response_model=LiveSessionStatus,
    dependencies=[Depends(require_internal_token)],
)
async def stop_live_session(
    request: LiveSessionRequest,
    live_service: LiveServiceDep,
) -> LiveSessionStatus:
    return await live_service.stop(request)


@router.get(
    "/v1/live/sessions/status",
    response_model=LiveSessionStatus,
    dependencies=[Depends(require_internal_token)],
)
async def live_session_status(
    live_service: LiveServiceDep,
    year: int = Query(...),
    event: str = Query(...),
    session: str = Query(...),
) -> LiveSessionStatus:
    return live_service.status(LiveSessionRequest(year=year, event=event, session=session))


@router.get(
    "/v1/live/sessions/snapshot",
    response_model=SessionBundle,
    dependencies=[Depends(require_internal_token)],
)
async def live_session_snapshot(
    live_service: LiveServiceDep,
    year: int = Query(...),
    event: str = Query(...),
    session: str = Query(...),
) -> SessionBundle | JSONResponse:
    payload = live_service.snapshot(LiveSessionRequest(year=year, event=event, session=session))
    if payload is None:
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"detail": "no snapshot available yet"})
    payload = {**payload, "cache_hit": False, "cache_key": "live"}
    return SessionBundle.model_validate(payload)
