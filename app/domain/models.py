from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    fastf1_cache_dir: str
    data_cache_dir: str
    worker_processes: int


class ProxySettingsRequest(BaseModel):
    https_proxy: str | None = None


class ProxySettingsResponse(BaseModel):
    https_proxy: str | None = None


class ServiceOverview(BaseModel):
    service: str
    mode: str
    transport: str
    capabilities: list[str]
    cache_layers: list[str]
    notes: list[str]


class ScheduleEvent(BaseModel):
    season_year: int
    round_number: int
    event_name: str
    event_format: str | None = None
    country: str | None = None
    location: str | None = None
    official_event_name: str | None = None
    event_date: str | None = None
    session_names: list[str] = Field(default_factory=list)


class ScheduleResponse(BaseModel):
    season_year: int
    events: list[ScheduleEvent]
    cache_hit: bool
    cache_key: str


class SessionRequest(BaseModel):
    year: int
    event: str | int
    session: str
    include_laps: bool = True
    include_weather: bool = True
    include_messages: bool = True
    include_telemetry: bool = False
    refresh: bool = False


class SessionDescriptor(BaseModel):
    season_year: int
    event_name: str
    event_official_name: str | None = None
    location: str | None = None
    country: str | None = None
    round_number: int | None = None
    session_name: str
    session_type: str
    scheduled_start: str | None = None
    scheduled_end: str | None = None


class DriverRef(BaseModel):
    driver_code: str
    full_name: str | None = None
    team_name: str | None = None
    team_color: str | None = None
    permanent_number: int | None = None


class SessionResultRow(BaseModel):
    driver_code: str
    position: int | None = None
    team_name: str | None = None
    full_name: str | None = None
    grid_position: int | None = None
    points: float | None = None
    status: str | None = None
    classified: bool | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class LapRow(BaseModel):
    driver_code: str
    lap_number: int
    stint: int | None = None
    lap_time_ms: int | None = None
    session_time_ms: int | None = None
    lap_finish_time_ms: int | None = None
    sector_1_ms: int | None = None
    sector_2_ms: int | None = None
    sector_3_ms: int | None = None
    compound: str | None = None
    tyre_life: int | None = None
    is_pit_out_lap: bool = False
    is_pit_in_lap: bool = False
    deleted: bool = False
    position: int | None = None


class StintRow(BaseModel):
    driver_code: str
    stint: int
    compound: str | None = None
    lap_start: int | None = None
    lap_end: int | None = None
    lap_count: int | None = None


class WeatherRow(BaseModel):
    timestamp: str | None = None
    air_temp: float | None = None
    track_temp: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    rainfall: bool | None = None
    wind_speed: float | None = None
    wind_direction: int | None = None


class RaceControlRow(BaseModel):
    timestamp: str | None = None
    category: str | None = None
    message: str | None = None
    status: str | None = None
    flag: str | None = None
    lap: int | None = None
    scope: str | None = None


class SessionBundle(BaseModel):
    descriptor: SessionDescriptor
    drivers: list[DriverRef]
    results: list[SessionResultRow]
    laps: list[LapRow]
    stints: list[StintRow]
    weather: list[WeatherRow]
    race_control: list[RaceControlRow]
    telemetry_available: bool
    position_data_available: bool
    cache_hit: bool
    cache_key: str


class TelemetryCompareRequest(BaseModel):
    year: int
    event: str | int
    session: str
    driver_a: str
    lap_a: int
    driver_b: str
    lap_b: int
    refresh: bool = False


class TelemetrySample(BaseModel):
    distance_m: float | None = None
    distance_pct: float
    time_ms: int | None = None
    speed: float | None = None
    throttle: float | None = None
    brake: bool | None = None
    rpm: float | None = None
    drs: int | None = None
    gear: int | None = None


class TelemetryLapDescriptor(BaseModel):
    driver_code: str
    lap_number: int
    lap_time_ms: int | None = None
    compound: str | None = None
    tyre_life: int | None = None
    stint: int | None = None


class TelemetryLapTrace(BaseModel):
    descriptor: TelemetryLapDescriptor
    samples: list[TelemetrySample]


class TelemetryCompareResponse(BaseModel):
    season_year: int
    round_number: int | None = None
    event_name: str
    session_name: str
    lap_a: TelemetryLapTrace
    lap_b: TelemetryLapTrace
    cache_hit: bool
    cache_key: str


class RacePlaybackRequest(BaseModel):
    year: int
    event: str | int
    session: str
    sample_step_ms: int = 1000
    window_start_ms: int | None = None
    window_end_ms: int | None = None
    include_telemetry: bool = False
    refresh: bool = False


class RacePlaybackPoint(BaseModel):
    x: float
    y: float


class RacePlaybackSample(BaseModel):
    time_ms: int
    x: float
    y: float
    status: str | None = None
    speed: float | None = None
    throttle: float | None = None
    brake: bool | None = None
    rpm: float | None = None
    drs: int | None = None
    gear: int | None = None


class RacePlaybackTrack(BaseModel):
    source: str
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    polyline: list[RacePlaybackPoint]


class RacePlaybackDriver(BaseModel):
    driver_code: str
    short_label: str
    full_name: str | None = None
    team_name: str | None = None
    team_color: str | None = None
    finish_position: int | None = None
    samples: list[RacePlaybackSample]


class RacePlaybackResponse(BaseModel):
    season_year: int
    round_number: int | None = None
    event_name: str
    session_name: str
    available: bool
    message: str | None = None
    sample_step_ms: int
    window_start_ms: int
    window_end_ms: int
    track: RacePlaybackTrack | None = None
    drivers: list[RacePlaybackDriver] = Field(default_factory=list)
    cache_hit: bool
    cache_key: str
