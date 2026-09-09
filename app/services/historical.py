from __future__ import annotations

import asyncio
import math
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from app.cache import CacheManager
from app.domain.models import (
    RacePlaybackRequest,
    RacePlaybackResponse,
    ScheduleResponse,
    SessionBundle,
    SessionRequest,
    TelemetryCompareRequest,
    TelemetryCompareResponse,
)


def _import_fastf1(cache_dir: str):
    import fastf1  # type: ignore

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(cache_path)
    return fastf1


def _slugify_event(event: str | int) -> str:
    if isinstance(event, int):
        return f"round-{event}"
    normalized = str(event).strip().lower()
    chars = []
    for char in normalized:
        chars.append(char if char.isalnum() else "-")
    return "".join(chars).strip("-")


def _cache_key_for_schedule(year: int) -> str:
    return f"schedule/{year}.json"


def _cache_key_for_session(request: SessionRequest) -> str:
    event_slug = _slugify_event(request.event)
    session_slug = request.session.strip().lower()
    suffix = []
    if request.include_laps:
        suffix.append("laps")
    if request.include_weather:
        suffix.append("weather")
    if request.include_messages:
        suffix.append("messages")
    if request.include_telemetry:
        suffix.append("telemetry")
    joined = "-".join(suffix) or "meta"
    return f"sessions/{request.year}/{event_slug}/{session_slug}-{joined}.json"


def _cache_key_for_telemetry(request: TelemetryCompareRequest) -> str:
    event_slug = _slugify_event(request.event)
    session_slug = request.session.strip().lower()
    return (
        f"telemetry/{request.year}/{event_slug}/{session_slug}/"
        f"{request.driver_a.lower()}-{request.lap_a}__{request.driver_b.lower()}-{request.lap_b}.json"
    )


def _cache_key_for_race_playback(request: RacePlaybackRequest) -> str:
    event_slug = _slugify_event(request.event)
    session_slug = request.session.strip().lower()
    window_suffix = ""
    if request.window_start_ms is not None or request.window_end_ms is not None:
        window_suffix = f"__{max(0, request.window_start_ms or 0)}-{max(0, request.window_end_ms or 0)}"
    return (
        f"playback/{request.year}/{event_slug}/{session_slug}/"
        f"race-playback-v7-{max(100, request.sample_step_ms)}ms{window_suffix}.json"
    )


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if hasattr(value, "isoformat"):
            return value.isoformat()
    except Exception:
        return None
    return str(value)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except Exception:
        pass
    try:
        return int(value)
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return None


def _timedelta_to_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if hasattr(value, "total_seconds"):
            return int(value.total_seconds() * 1000)
    except Exception:
        return None
    return None


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except Exception:
        pass
    return bool(value)


def _normalize_hex_color(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized if normalized.startswith("#") else f"#{normalized}"


def _clean_dict(raw: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            cleaned[key] = None
            continue
        if hasattr(value, "isoformat"):
            cleaned[key] = _to_iso(value)
            continue
        if hasattr(value, "total_seconds"):
            cleaned[key] = _timedelta_to_ms(value)
            continue
        try:
            if math.isnan(value):
                cleaned[key] = None
                continue
        except Exception:
            pass
        if isinstance(value, (str, int, float, bool, list, dict)):
            cleaned[key] = value
        else:
            cleaned[key] = str(value)
    return cleaned


def _safe_session_attr(session: Any, attr: str) -> Any | None:
    try:
        return getattr(session, attr, None)
    except Exception:
        return None


def _build_telemetry_samples(frame: Any) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []

    distance_max = _to_float(frame["Distance"].max()) if "Distance" in frame else None
    samples: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        distance_m = _to_float(row.get("Distance"))
        distance_pct = 0.0
        if distance_m is not None and distance_max and distance_max > 0:
            distance_pct = (distance_m / distance_max) * 100.0
        elif len(samples) > 0:
            distance_pct = samples[-1]["distance_pct"]
        samples.append(
            {
                "distance_m": distance_m,
                "distance_pct": distance_pct,
                "time_ms": _timedelta_to_ms(row.get("Time")),
                "speed": _to_float(row.get("Speed")),
                "throttle": _to_float(row.get("Throttle")),
                "brake": _to_bool(row.get("Brake")),
                "rpm": _to_float(row.get("RPM")),
                "drs": _to_int(row.get("DRS")),
                "gear": _to_int(row.get("nGear")),
            }
        )

    if len(samples) <= 500:
        return samples

    step = max(1, len(samples) // 500)
    return [sample for index, sample in enumerate(samples) if index % step == 0 or index == len(samples) - 1]


def _pick_lap_reference(laps_frame: Any, driver_code: str, lap_number: int) -> Any | None:
    try:
        driver_laps = laps_frame.pick_drivers(driver_code)
        filtered = driver_laps[driver_laps["LapNumber"] == lap_number]
        if len(filtered) == 0:
            return None
        preferred = filtered[filtered["Deleted"] != True]  # noqa: E712
        if len(preferred) > 0:
            return preferred.iloc[0]
        return filtered.iloc[0]
    except Exception:
        return None


def _extract_position_points(frame: Any, *, on_track_only: bool) -> list[dict[str, float]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []

    points: list[dict[str, float]] = []
    last_x: float | None = None
    last_y: float | None = None
    for _, row in frame.iterrows():
        x = _to_float(row.get("X"))
        y = _to_float(row.get("Y"))
        if x is None or y is None:
            continue
        status = row.get("Status")
        if on_track_only and status and str(status).lower() == "offtrack":
            continue
        if last_x == x and last_y == y:
            continue
        points.append({"x": x, "y": y})
        last_x = x
        last_y = y

    return points


def _reduce_points(points: list[dict[str, float]], max_points: int) -> list[dict[str, float]]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    reduced = [point for index, point in enumerate(points) if index % step == 0 or index == len(points) - 1]
    if reduced[-1] != points[-1]:
        reduced.append(points[-1])
    return reduced


def _pick_reference_track_polyline(session: Any, laps_frame: Any) -> tuple[list[dict[str, float]], str]:
    if laps_frame is not None and hasattr(laps_frame, "sort_values") and hasattr(laps_frame, "iterlaps"):
        try:
            ordered = laps_frame.sort_values("LapTime")
            for _, lap in ordered.iterlaps(require=["LapTime"]):
                if bool(lap.get("Deleted")):
                    continue
                try:
                    lap_frame = lap.get_pos_data()
                except Exception:
                    continue
                points = _extract_position_points(lap_frame, on_track_only=True)
                if len(points) >= 40:
                    return _reduce_points(points, max_points=900), "reference_lap"
        except Exception:
            pass

    pos_data = _safe_session_attr(session, "pos_data")
    if isinstance(pos_data, dict):
        for driver_code in pos_data:
            points = _extract_position_points(pos_data.get(driver_code), on_track_only=True)
            if len(points) >= 80:
                return _reduce_points(points[:2400], max_points=900), "session_stream"

    return [], "unavailable"


def _downsample_position_samples(
    frame: Any,
    sample_step_ms: int,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []

    samples: list[dict[str, Any]] = []
    last_time_ms: int | None = None
    last_row: dict[str, Any] | None = None

    for _, row in frame.iterrows():
        time_ms = _timedelta_to_ms(row.get("SessionTime") or row.get("Time"))
        x = _to_float(row.get("X"))
        y = _to_float(row.get("Y"))
        if time_ms is None or x is None or y is None:
            continue
        if window_end_ms is not None and time_ms > window_end_ms:
            if last_row is not None:
                break
            break
        current = {
            "time_ms": time_ms,
            "x": x,
            "y": y,
            "status": str(row.get("Status")) if row.get("Status") is not None else None,
        }
        last_row = current
        if window_start_ms is not None and time_ms < window_start_ms:
            continue
        if last_time_ms is not None and (time_ms - last_time_ms) < sample_step_ms:
            continue
        samples.append(current)
        last_time_ms = time_ms

    if last_row is not None and (not samples or samples[-1]["time_ms"] != last_row["time_ms"]):
        samples.append(last_row)

    return samples


def _downsample_car_samples(
    frame: Any,
    sample_step_ms: int,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []

    samples: list[dict[str, Any]] = []
    last_time_ms: int | None = None
    last_row: dict[str, Any] | None = None

    for _, row in frame.iterrows():
        time_ms = _timedelta_to_ms(row.get("Time") or row.get("SessionTime"))
        if time_ms is None:
            continue
        if window_end_ms is not None and time_ms > window_end_ms:
            if last_row is not None:
                break
            break
        current = {
            "time_ms": time_ms,
            "speed": _to_float(row.get("Speed")),
            "throttle": _to_float(row.get("Throttle")),
            "brake": _to_bool(row.get("Brake")),
            "rpm": _to_float(row.get("RPM")),
            "drs": _to_int(row.get("DRS")),
            "gear": _to_int(row.get("nGear")),
        }
        last_row = current
        if window_start_ms is not None and time_ms < window_start_ms:
            continue
        if last_time_ms is not None and (time_ms - last_time_ms) < sample_step_ms:
            continue
        samples.append(current)
        last_time_ms = time_ms

    if last_row is not None and (not samples or samples[-1]["time_ms"] != last_row["time_ms"]):
        samples.append(last_row)

    return samples


def _driver_code_for_lap(lap: Any) -> str:
    for key in ("Driver", "DriverNumber"):
        value = lap.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().upper()
    return ""


def _session_position_streams(
    session: Any,
    laps_frame: Any,
    sample_step_ms: int,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    streams: dict[str, list[dict[str, Any]]] = {}
    pos_data = _safe_session_attr(session, "pos_data")
    if isinstance(pos_data, dict):
        for driver_key, frame in pos_data.items():
            samples = _downsample_position_samples(
                frame,
                sample_step_ms=sample_step_ms,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            )
            if samples:
                streams[str(driver_key).strip().upper()] = samples
    if streams:
        return streams

    if laps_frame is None or not hasattr(laps_frame, "iterlaps"):
        return streams

    try:
        iterator = laps_frame.sort_values(["Driver", "LapNumber"]).iterlaps()
    except Exception:
        iterator = laps_frame.iterlaps()

    for _, lap in iterator:
        driver_code = _driver_code_for_lap(lap)
        if not driver_code:
            continue
        try:
            frame = lap.get_pos_data()
        except Exception:
            continue
        samples = _downsample_position_samples(
            frame,
            sample_step_ms=sample_step_ms,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        if not samples:
            continue
        streams.setdefault(driver_code, []).extend(samples)

    for driver_code, samples in list(streams.items()):
        samples.sort(key=lambda item: item["time_ms"])
        deduped: list[dict[str, Any]] = []
        seen: set[int] = set()
        for sample in samples:
            time_ms = int(sample["time_ms"])
            if time_ms in seen:
                continue
            seen.add(time_ms)
            deduped.append(sample)
        streams[driver_code] = deduped

    return {driver_code: samples for driver_code, samples in streams.items() if len(samples) >= 2}


def _session_car_streams(
    session: Any,
    sample_step_ms: int,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    streams: dict[str, list[dict[str, Any]]] = {}
    car_data = _safe_session_attr(session, "car_data")
    if isinstance(car_data, dict):
        for driver_key, frame in car_data.items():
            samples = _downsample_car_samples(
                frame,
                sample_step_ms=sample_step_ms,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            )
            if samples:
                streams[str(driver_key).strip().upper()] = samples
    return streams


def _attach_telemetry_to_position_samples(
    position_samples: list[dict[str, Any]],
    telemetry_samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not position_samples or not telemetry_samples:
        return position_samples

    enriched: list[dict[str, Any]] = []
    telemetry_index = 0
    for sample in position_samples:
        time_ms = int(sample.get("time_ms") or 0)
        while (
            telemetry_index + 1 < len(telemetry_samples)
            and telemetry_samples[telemetry_index + 1]["time_ms"] <= time_ms
        ):
            telemetry_index += 1

        nearest = telemetry_samples[telemetry_index]
        if telemetry_index + 1 < len(telemetry_samples):
            candidate = telemetry_samples[telemetry_index + 1]
            if abs(int(candidate["time_ms"]) - time_ms) < abs(int(nearest["time_ms"]) - time_ms):
                nearest = candidate

        enriched.append({
            **sample,
            "speed": nearest.get("speed"),
            "throttle": nearest.get("throttle"),
            "brake": nearest.get("brake"),
            "rpm": nearest.get("rpm"),
            "drs": nearest.get("drs"),
            "gear": nearest.get("gear"),
        })

    return enriched


def _track_bounds(
    track_points: list[dict[str, float]],
    driver_series: list[dict[str, Any]],
) -> tuple[float, float, float, float]:
    xs = [point["x"] for point in track_points]
    ys = [point["y"] for point in track_points]
    for driver in driver_series:
        for sample in driver.get("samples", []):
            xs.append(sample["x"])
            ys.append(sample["y"])

    if not xs or not ys:
        return -1.0, 1.0, -1.0, 1.0

    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)
    pad_x = max(100.0, span_x * 0.08)
    pad_y = max(100.0, span_y * 0.08)
    return min_x - pad_x, max_x + pad_x, min_y - pad_y, max_y + pad_y


def _estimate_race_start_ms(laps_frame: Any) -> int | None:
    if laps_frame is None or not hasattr(laps_frame, "iterrows"):
        return None

    candidates: list[int] = []
    for _, row in laps_frame.iterrows():
        lap_number = _to_int(row.get("LapNumber"))
        if lap_number != 1:
            continue
        lap_finish_ms = _timedelta_to_ms(row.get("SessionTime") or row.get("Time"))
        lap_time_ms = _timedelta_to_ms(row.get("LapTime"))
        if lap_finish_ms is None or lap_time_ms is None:
            continue
        candidates.append(max(0, lap_finish_ms - lap_time_ms))

    if not candidates:
        return None
    return min(candidates)


def fetch_telemetry_compare_payload(request_payload: dict[str, Any], cache_dir: str) -> dict[str, Any]:
    fastf1 = _import_fastf1(cache_dir)

    request = TelemetryCompareRequest.model_validate(request_payload)
    session = fastf1.get_session(request.year, request.event, request.session)
    session.load(
        laps=True,
        telemetry=True,
        weather=False,
        messages=False,
    )

    event = getattr(session, "event", None)
    laps_frame = _safe_session_attr(session, "laps")
    if laps_frame is None:
        raise RuntimeError("session laps are not available")

    def build_trace(driver_code: str, lap_number: int) -> dict[str, Any]:
        lap_ref = _pick_lap_reference(laps_frame, driver_code, lap_number)
        if lap_ref is None:
            raise RuntimeError(f"lap {driver_code} #{lap_number} was not found")

        telemetry = lap_ref.get_car_data()
        if hasattr(telemetry, "add_distance"):
            telemetry = telemetry.add_distance()

        return {
            "descriptor": {
                "driver_code": driver_code,
                "lap_number": lap_number,
                "lap_time_ms": _timedelta_to_ms(lap_ref.get("LapTime")),
                "compound": lap_ref.get("Compound"),
                "tyre_life": _to_int(lap_ref.get("TyreLife")),
                "stint": _to_int(lap_ref.get("Stint")),
            },
            "samples": _build_telemetry_samples(telemetry),
        }

    return {
        "season_year": request.year,
        "round_number": _to_int(getattr(event, "RoundNumber", None)),
        "event_name": getattr(event, "EventName", None) or str(request.event),
        "session_name": getattr(session, "name", request.session),
        "lap_a": build_trace(request.driver_a, request.lap_a),
        "lap_b": build_trace(request.driver_b, request.lap_b),
    }


def fetch_race_playback_payload(request_payload: dict[str, Any], cache_dir: str) -> dict[str, Any]:
    fastf1 = _import_fastf1(cache_dir)

    request = RacePlaybackRequest.model_validate(request_payload)
    sample_step_ms = max(100, request.sample_step_ms)
    session = fastf1.get_session(request.year, request.event, request.session)
    session.load(
        laps=True,
        telemetry=True,
        weather=False,
        messages=False,
    )

    event = getattr(session, "event", None)
    response: dict[str, Any] = {
        "season_year": request.year,
        "round_number": _to_int(getattr(event, "RoundNumber", None)),
        "event_name": getattr(event, "EventName", None) or str(request.event),
        "session_name": getattr(session, "name", request.session),
        "available": False,
        "message": "Position data is not available for this session yet.",
        "sample_step_ms": sample_step_ms,
        "window_start_ms": 0,
        "window_end_ms": 0,
        "track": None,
        "drivers": [],
    }

    laps_frame = _safe_session_attr(session, "laps")
    results_frame = _safe_session_attr(session, "results")
    playback_start_ms = _estimate_race_start_ms(laps_frame)
    raw_window_start_ms: int | None = None
    raw_window_end_ms: int | None = None
    if playback_start_ms is not None:
        if request.window_start_ms is not None:
            raw_window_start_ms = max(
                playback_start_ms,
                playback_start_ms + max(0, request.window_start_ms) - sample_step_ms,
            )
        if request.window_end_ms is not None:
            raw_window_end_ms = max(
                playback_start_ms,
                playback_start_ms + max(0, request.window_end_ms) + sample_step_ms,
            )

    position_streams = _session_position_streams(
        session,
        laps_frame,
        sample_step_ms=sample_step_ms,
        window_start_ms=raw_window_start_ms,
        window_end_ms=raw_window_end_ms,
    )
    if not position_streams:
        return response
    car_streams = _session_car_streams(
        session,
        sample_step_ms=sample_step_ms,
        window_start_ms=raw_window_start_ms,
        window_end_ms=raw_window_end_ms,
    ) if request.include_telemetry else {}

    result_rows: dict[str, dict[str, Any]] = {}
    driver_number_to_code: dict[str, str] = {}
    if results_frame is not None and hasattr(results_frame, "iterrows"):
        for _, row in results_frame.iterrows():
            driver_code = str(row.get("Abbreviation") or "").strip().upper()
            driver_number = str(row.get("DriverNumber") or "").strip()
            if not driver_code and not driver_number:
                continue
            normalized_code = driver_code or driver_number
            result_rows[normalized_code] = {
                "full_name": row.get("FullName"),
                "team_name": row.get("TeamName"),
                "team_color": _normalize_hex_color(row.get("TeamColor")),
                "finish_position": _to_int(row.get("Position")),
            }
            if driver_number:
                driver_number_to_code[driver_number] = normalized_code

    driver_series: list[dict[str, Any]] = []
    for driver_key, stream_samples in position_streams.items():
        samples = list(stream_samples)
        if playback_start_ms is not None:
            samples = [sample for sample in samples if sample["time_ms"] >= playback_start_ms]
        if len(samples) < 2:
            continue
        normalized_code = driver_number_to_code.get(str(driver_key), str(driver_key).strip().upper())
        if request.include_telemetry:
            telemetry_samples = (
                car_streams.get(normalized_code)
                or car_streams.get(str(driver_key).strip().upper())
                or []
            )
            if telemetry_samples:
                samples = _attach_telemetry_to_position_samples(samples, telemetry_samples)
        meta = result_rows.get(normalized_code, {})
        driver_series.append(
            {
                "driver_code": normalized_code,
                "short_label": normalized_code,
                "full_name": meta.get("full_name"),
                "team_name": meta.get("team_name"),
                "team_color": meta.get("team_color"),
                "finish_position": meta.get("finish_position"),
                "samples": samples,
            }
        )

    if not driver_series:
        response["message"] = "Position data could not be decoded into driver traces."
        return response

    time_offset_ms = playback_start_ms
    if time_offset_ms is None:
        time_offset_ms = min((driver["samples"][0]["time_ms"] for driver in driver_series), default=0)

    for driver in driver_series:
        normalized_samples: list[dict[str, Any]] = []
        for sample in driver["samples"]:
            normalized_sample = dict(sample)
            normalized_sample["time_ms"] = max(0, int(sample["time_ms"]) - time_offset_ms)
            normalized_samples.append(normalized_sample)
        if request.window_start_ms is not None or request.window_end_ms is not None:
            normalized_samples = [
                sample for sample in normalized_samples
                if (
                    request.window_start_ms is None
                    or sample["time_ms"] >= max(0, request.window_start_ms - sample_step_ms)
                )
                and (
                    request.window_end_ms is None
                    or sample["time_ms"] <= max(0, request.window_end_ms + sample_step_ms)
                )
            ]
        driver["samples"] = normalized_samples

    driver_series.sort(key=lambda item: (item.get("finish_position") or 999, item["driver_code"]))
    track_points, track_source = _pick_reference_track_polyline(session, laps_frame)
    if not track_points:
        response["message"] = "Track shape is not available for this session."
        return response

    min_x, max_x, min_y, max_y = _track_bounds(track_points, driver_series)
    window_start_ms = 0
    window_end_ms = max((driver["samples"][-1]["time_ms"] for driver in driver_series), default=0)

    response.update(
        {
            "available": True,
            "message": None,
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "track": {
                "source": track_source,
                "min_x": min_x,
                "max_x": max_x,
                "min_y": min_y,
                "max_y": max_y,
                "polyline": track_points,
            },
            "drivers": driver_series,
        }
    )
    return response


def fetch_schedule_payload(year: int, cache_dir: str) -> dict[str, Any]:
    fastf1 = _import_fastf1(cache_dir)

    import pandas as pd  # type: ignore

    schedule = fastf1.get_event_schedule(year, include_testing=False)
    events: list[dict[str, Any]] = []
    session_columns = [
        column for column in schedule.columns if column.startswith("Session") and not column.endswith("Date")
    ]

    for _, row in schedule.iterrows():
        session_names = []
        for column in session_columns:
            value = row.get(column)
            if isinstance(value, str) and value.strip():
                session_names.append(value.strip())

        event_date = row.get("EventDate")
        if pd.notna(event_date) and hasattr(event_date, "isoformat"):
            event_date_value = event_date.date().isoformat() if hasattr(event_date, "date") else event_date.isoformat()
        else:
            event_date_value = None

        events.append(
            {
                "season_year": year,
                "round_number": _to_int(row.get("RoundNumber")) or 0,
                "event_name": row.get("EventName") or "",
                "event_format": row.get("EventFormat"),
                "country": row.get("Country"),
                "location": row.get("Location"),
                "official_event_name": row.get("OfficialEventName"),
                "event_date": event_date_value,
                "session_names": session_names,
            }
        )

    return {
        "season_year": year,
        "events": events,
    }


def fetch_session_payload(request_payload: dict[str, Any], cache_dir: str) -> dict[str, Any]:
    fastf1 = _import_fastf1(cache_dir)

    request = SessionRequest.model_validate(request_payload)
    session = fastf1.get_session(request.year, request.event, request.session)
    session.load(
        laps=request.include_laps,
        telemetry=request.include_telemetry,
        weather=request.include_weather,
        messages=request.include_messages,
    )

    event = getattr(session, "event", None)
    descriptor = {
        "season_year": request.year,
        "event_name": getattr(event, "EventName", None) or str(request.event),
        "event_official_name": getattr(event, "OfficialEventName", None),
        "location": getattr(event, "Location", None),
        "country": getattr(event, "Country", None),
        "round_number": _to_int(getattr(event, "RoundNumber", None)),
        "session_name": getattr(session, "name", request.session),
        "session_type": request.session.upper(),
        "scheduled_start": _to_iso(getattr(session, "date", None)),
        "scheduled_end": _to_iso(getattr(session, "session_end_time", None)),
    }

    drivers: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    results_frame = _safe_session_attr(session, "results")
    if results_frame is not None and hasattr(results_frame, "iterrows"):
        for _, row in results_frame.iterrows():
            driver_code = row.get("Abbreviation") or row.get("DriverNumber") or ""
            driver_item = {
                "driver_code": str(driver_code),
                "full_name": row.get("FullName"),
                "team_name": row.get("TeamName"),
                "team_color": row.get("TeamColor"),
                "permanent_number": _to_int(row.get("DriverNumber")),
            }
            if not any(item["driver_code"] == driver_item["driver_code"] for item in drivers):
                drivers.append(driver_item)

            results.append(
                {
                    "driver_code": str(driver_code),
                    "position": _to_int(row.get("Position")),
                    "team_name": row.get("TeamName"),
                    "full_name": row.get("FullName"),
                    "grid_position": _to_int(row.get("GridPosition")),
                    "points": _to_float(row.get("Points")),
                    "status": row.get("Status"),
                    "classified": _to_bool(row.get("ClassifiedPosition")),
                    "raw": _clean_dict(row.to_dict()),
                }
            )

    laps: list[dict[str, Any]] = []
    stints: list[dict[str, Any]] = []
    laps_frame = _safe_session_attr(session, "laps")
    if request.include_laps and laps_frame is not None and hasattr(laps_frame, "iterrows"):
        stint_index: dict[tuple[str, int], dict[str, Any]] = {}

        for _, row in laps_frame.iterrows():
            driver_code = str(row.get("Driver", ""))
            stint_no = _to_int(row.get("Stint"))
            compound = row.get("Compound")
            lap_number = _to_int(row.get("LapNumber"))
            if lap_number is None:
                continue

            laps.append(
                {
                    "driver_code": driver_code,
                    "lap_number": lap_number,
                    "stint": stint_no,
                    "lap_time_ms": _timedelta_to_ms(row.get("LapTime")),
                    "session_time_ms": _timedelta_to_ms(row.get("SessionTime")),
                    "lap_finish_time_ms": _timedelta_to_ms(row.get("Time")),
                    "sector_1_ms": _timedelta_to_ms(row.get("Sector1Time")),
                    "sector_2_ms": _timedelta_to_ms(row.get("Sector2Time")),
                    "sector_3_ms": _timedelta_to_ms(row.get("Sector3Time")),
                    "compound": compound,
                    "tyre_life": _to_int(row.get("TyreLife")),
                    "is_pit_out_lap": bool(row.get("PitOutTime") is not None),
                    "is_pit_in_lap": bool(row.get("PitInTime") is not None),
                    "deleted": bool(row.get("Deleted") or False),
                    "position": _to_int(row.get("Position")),
                }
            )

            if stint_no is None:
                continue

            key = (driver_code, stint_no)
            entry = stint_index.get(key)
            if entry is None:
                stint_index[key] = {
                    "driver_code": driver_code,
                    "stint": stint_no,
                    "compound": compound,
                    "lap_start": lap_number,
                    "lap_end": lap_number,
                    "lap_count": 1,
                }
            else:
                entry["lap_end"] = lap_number
                entry["lap_count"] = int(entry["lap_count"] or 0) + 1

        stints = sorted(stint_index.values(), key=lambda item: (item["driver_code"], item["stint"]))

    weather: list[dict[str, Any]] = []
    weather_frame = _safe_session_attr(session, "weather_data")
    if request.include_weather and weather_frame is not None and hasattr(weather_frame, "iterrows"):
        for _, row in weather_frame.iterrows():
            weather.append(
                {
                    "timestamp": _to_iso(row.get("Time")),
                    "air_temp": _to_float(row.get("AirTemp")),
                    "track_temp": _to_float(row.get("TrackTemp")),
                    "humidity": _to_float(row.get("Humidity")),
                    "pressure": _to_float(row.get("Pressure")),
                    "rainfall": _to_bool(row.get("Rainfall")),
                    "wind_speed": _to_float(row.get("WindSpeed")),
                    "wind_direction": _to_int(row.get("WindDirection")),
                }
            )

    race_control: list[dict[str, Any]] = []
    messages_frame = _safe_session_attr(session, "race_control_messages")
    if request.include_messages and messages_frame is not None and hasattr(messages_frame, "iterrows"):
        for _, row in messages_frame.iterrows():
            race_control.append(
                {
                    "timestamp": _to_iso(row.get("Time")),
                    "category": row.get("Category"),
                    "message": row.get("Message"),
                    "status": row.get("Status"),
                    "flag": row.get("Flag"),
                    "lap": _to_int(row.get("Lap")),
                    "scope": row.get("Scope"),
                }
            )

    telemetry_available = request.include_telemetry
    position_data_available = request.include_telemetry

    if request.include_telemetry:
        try:
            telemetry_available = bool(_safe_session_attr(session, "car_data"))
        except Exception:
            telemetry_available = False
        try:
            position_data_available = bool(_safe_session_attr(session, "pos_data"))
        except Exception:
            position_data_available = False

    return {
        "descriptor": descriptor,
        "drivers": drivers,
        "results": results,
        "laps": laps,
        "stints": stints,
        "weather": weather,
        "race_control": race_control,
        "telemetry_available": telemetry_available,
        "position_data_available": position_data_available,
    }


class HistoricalService:
    def __init__(self, fastf1_cache_dir: Path, cache: CacheManager, worker_processes: int) -> None:
        self.fastf1_cache_dir = fastf1_cache_dir
        self.cache = cache
        self.executor = ProcessPoolExecutor(max_workers=worker_processes)

    async def get_schedule(self, year: int, refresh: bool = False) -> ScheduleResponse:
        cache_key = _cache_key_for_schedule(year)
        if not refresh:
            cached = self.cache.read_json(cache_key)
            if cached is not None:
                cached["cache_hit"] = True
                cached["cache_key"] = cache_key
                return ScheduleResponse.model_validate(cached)

        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(self.executor, fetch_schedule_payload, year, str(self.fastf1_cache_dir))
        self.cache.write_json(cache_key, payload)
        payload["cache_hit"] = False
        payload["cache_key"] = cache_key
        return ScheduleResponse.model_validate(payload)

    async def load_session(self, request: SessionRequest) -> SessionBundle:
        cache_key = _cache_key_for_session(request)
        if not request.refresh:
            cached = self.cache.read_json(cache_key)
            if cached is not None:
                cached["cache_hit"] = True
                cached["cache_key"] = cache_key
                return SessionBundle.model_validate(cached)

        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            self.executor,
            fetch_session_payload,
            request.model_dump(),
            str(self.fastf1_cache_dir),
        )
        self.cache.write_json(cache_key, payload)
        payload["cache_hit"] = False
        payload["cache_key"] = cache_key
        return SessionBundle.model_validate(payload)

    async def telemetry_compare(self, request: TelemetryCompareRequest) -> TelemetryCompareResponse:
        cache_key = _cache_key_for_telemetry(request)
        if not request.refresh:
            cached = self.cache.read_json(cache_key)
            if cached is not None:
                cached["cache_hit"] = True
                cached["cache_key"] = cache_key
                return TelemetryCompareResponse.model_validate(cached)

        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            self.executor,
            fetch_telemetry_compare_payload,
            request.model_dump(mode="json"),
            str(self.fastf1_cache_dir),
        )
        self.cache.write_json(cache_key, payload)
        payload["cache_hit"] = False
        payload["cache_key"] = cache_key
        return TelemetryCompareResponse.model_validate(payload)

    async def race_playback(self, request: RacePlaybackRequest) -> RacePlaybackResponse:
        cache_key = _cache_key_for_race_playback(request)
        if not request.refresh:
            cached = self.cache.read_json(cache_key)
            if cached is not None:
                if cached.get("available") is False:
                    cached = None
                else:
                    cached["cache_hit"] = True
                    cached["cache_key"] = cache_key
                    return RacePlaybackResponse.model_validate(cached)

        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            self.executor,
            fetch_race_playback_payload,
            request.model_dump(mode="json"),
            str(self.fastf1_cache_dir),
        )
        if payload.get("available") is not False:
            self.cache.write_json(cache_key, payload)
        payload["cache_hit"] = False
        payload["cache_key"] = cache_key
        return RacePlaybackResponse.model_validate(payload)
