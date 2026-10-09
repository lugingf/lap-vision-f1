"""The state of a session as the live-timing feed describes it, kept up to date message by message.

The feed is a stream of topics. The first message of a topic is the whole object, every later one is
a change to it, and a list inside it is changed by a dict keyed with the index of the item
("Sectors": {"1": {...}}). Two topics, CarData.z and Position.z, arrive compressed. This module holds
no connection and no clock: it is given messages and answers with what they add up to, which is what
lets it be tried against a recording of a race.
"""

from __future__ import annotations

import base64
import json
import math
import zlib
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

# What a mini-sector's status number means on the broadcast: green is a personal best, purple an
# overall best, yellow a slower one. Anything else is drawn as not run, or as the pit lane.
SEGMENT_COLOR = {
    2048: "yellow",
    2049: "green",
    2051: "purple",
    2064: "pit",
}

TRACK_STATUS = {
    "1": "clear",
    "2": "yellow",
    "3": "unknown",
    "4": "safety_car",
    "5": "red",
    "6": "virtual_safety_car",
    "7": "virtual_safety_car_ending",
}

# Car-data channels, as the feed numbers them.
_CHANNEL_RPM = "0"
_CHANNEL_SPEED = "2"
_CHANNEL_GEAR = "3"
_CHANNEL_THROTTLE = "4"
_CHANNEL_BRAKE = "5"
_CHANNEL_DRS = "45"

# How much of a car's recent past is kept, in samples. Car data comes about four times a second and
# position about twice, so these are a couple of minutes and a minute.
CAR_HISTORY = 900
POSITION_HISTORY = 240

RADIO_KEPT = 40
RACE_CONTROL_KEPT = 200


def merge(target: Any, change: Any) -> Any:
    """Apply one message to what is already known and answer the result.

    A dict changes a dict key by key. A dict applied to a list is a change to the items it names by
    index. Anything else replaces what was there.
    """
    if isinstance(change, dict):
        if isinstance(target, list):
            for key, value in change.items():
                if not str(key).isdigit():
                    continue
                index = int(key)
                while len(target) <= index:
                    target.append({})
                target[index] = merge(target[index], value) if isinstance(value, (dict, list)) else value
            return target
        if not isinstance(target, dict):
            target = {}
        for key, value in change.items():
            if key == "_kf":
                continue
            if isinstance(value, (dict, list)):
                target[key] = merge(target.get(key), value)
            else:
                target[key] = value
        return target
    if isinstance(change, list):
        return deepcopy(change)
    return change


def decode_compressed(payload: str) -> Any:
    """CarData.z and Position.z: base64 of a raw deflate stream of JSON."""
    raw = zlib.decompress(base64.b64decode(payload), -zlib.MAX_WBITS)
    return json.loads(raw.decode("utf-8-sig"))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _value(node: Any) -> str | None:
    """The 'Value' of a {"Value": ...} cell, or the cell itself when it is plain; None when empty."""
    if isinstance(node, dict):
        node = node.get("Value")
    if node is None or node == "":
        return None
    return str(node)


def _parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        # Seven fractional digits are what the feed writes; Python reads six.
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())[:6]
        try:
            return datetime.fromisoformat(f"{head}.{digits}+00:00").astimezone(UTC)
        except ValueError:
            return None


OUTLINE_MIN_STEP = 60.0
OUTLINE_LIMIT = 2400
OUTLINE_CLOSE_AFTER = 10000.0
OUTLINE_CLOSE_WITHIN = 400.0
OUTLINE_LOOKBACK = 60
OUTLINE_MIN_POINTS = 150


class Outline:
    """A circuit's outline drawn by one car going round: a point whenever it has moved far enough
    from the last, until it comes back to where it started. Coordinates are the feed's, tenths of a
    metre."""

    def __init__(self) -> None:
        self.points: list[tuple[float, float]] = []
        self.travelled = 0.0
        self.closed = False
        self.car: str | None = None

    def add(self, number: str, x: float | None, y: float | None) -> bool:
        if x is None or y is None or self.closed or len(self.points) >= OUTLINE_LIMIT:
            return False
        if self.car is None:
            self.car = number
        if number != self.car:
            return False
        step = 0.0
        if self.points:
            last = self.points[-1]
            step = math.hypot(x - last[0], y - last[1])
            if step < OUTLINE_MIN_STEP:
                return False
        self.travelled += step
        if self.travelled > OUTLINE_CLOSE_AFTER and len(self.points) >= OUTLINE_MIN_POINTS:
            for px, py in self.points[:OUTLINE_LOOKBACK]:
                if math.hypot(x - px, y - py) < OUTLINE_CLOSE_WITHIN:
                    self.points.append(self.points[0])
                    self.closed = True
                    return True
        self.points.append((x, y))
        return False

    def load(self, points: list[dict[str, float]]) -> None:
        self.points = [(float(point["x"]), float(point["y"])) for point in points]
        self.closed = True

    def payload(self, circuit: str | None) -> dict[str, Any]:
        return {
            "circuit": circuit,
            "closed": self.closed,
            "polyline": [{"x": x, "y": y} for x, y in self.points],
        }


class LiveState:
    """Everything the feed has said about one session so far."""

    def __init__(self) -> None:
        self.messages = 0
        self.last_message_at: datetime | None = None
        self.topics: dict[str, int] = {}

        self.session_info: dict[str, Any] = {}
        self.session_status: str | None = None
        self.lap_count: dict[str, Any] = {}
        self.clock: dict[str, Any] = {}
        self.track_status: dict[str, Any] = {}
        self.weather: dict[str, Any] = {}
        self.drivers: dict[str, dict[str, Any]] = {}
        self.timing: dict[str, dict[str, Any]] = {}
        self.app: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.top_three: list[dict[str, Any]] = []
        self.race_control: list[Any] = []
        self.radio: list[Any] = []
        self.session_data: dict[str, Any] = {}

        self.car: dict[str, dict[str, Any]] = {}
        self.car_history: dict[str, deque[dict[str, Any]]] = {}
        self.position: dict[str, dict[str, Any]] = {}
        self.position_history: dict[str, deque[dict[str, Any]]] = {}
        self.laps: dict[str, list[dict[str, Any]]] = {}
        self._lap_seen: dict[str, int] = {}
        self.lap_started: dict[str, str] = {}
        self.outline = Outline()

    # ------------------------------------------------------------------ input

    def feed(self, topic: str, payload: Any, stamp: str | None = None) -> None:
        """One message of the feed. A message that cannot be read is ignored: a live feed has
        nothing to gain from stopping at a bad line."""
        self.messages += 1
        self.topics[topic] = self.topics.get(topic, 0) + 1
        moment = _parse_utc(stamp)
        if moment is not None:
            self.last_message_at = moment

        if isinstance(payload, str) and not topic.endswith(".z"):
            try:
                payload = json.loads(payload)
            except ValueError:
                return
        try:
            handler = getattr(self, "_on_" + topic.replace(".", "_").lower(), None)
            if handler is not None:
                handler(payload)
        except Exception:  # noqa: BLE001 - one malformed message must not end the feed
            return

    def _on_sessioninfo(self, data: dict[str, Any]) -> None:
        self.session_info = merge(self.session_info, data)

    def _on_sessionstatus(self, data: dict[str, Any]) -> None:
        status = data.get("Status") or data.get("Started")
        if status:
            self.session_status = str(status)

    def _on_lapcount(self, data: dict[str, Any]) -> None:
        self.lap_count = merge(self.lap_count, data)

    def _on_extrapolatedclock(self, data: dict[str, Any]) -> None:
        self.clock = merge(self.clock, data)

    def _on_trackstatus(self, data: dict[str, Any]) -> None:
        self.track_status = merge(self.track_status, data)

    def _on_weatherdata(self, data: dict[str, Any]) -> None:
        self.weather = merge(self.weather, data)

    def _on_driverlist(self, data: dict[str, Any]) -> None:
        for number, info in data.items():
            if isinstance(info, dict):
                self.drivers[str(number)] = merge(self.drivers.get(str(number), {}), info)

    def _on_timingdata(self, data: dict[str, Any]) -> None:
        for number, change in (data.get("Lines") or {}).items():
            number = str(number)
            before = self.timing.get(number, {})
            self.timing[number] = merge(before, change)
            self._note_lap(number)

    def _on_timingappdata(self, data: dict[str, Any]) -> None:
        for number, change in (data.get("Lines") or {}).items():
            self.app[str(number)] = merge(self.app.get(str(number), {}), change)

    def _on_timingstats(self, data: dict[str, Any]) -> None:
        for number, change in (data.get("Lines") or {}).items():
            self.stats[str(number)] = merge(self.stats.get(str(number), {}), change)

    def _on_topthree(self, data: dict[str, Any]) -> None:
        lines = data.get("Lines")
        if isinstance(lines, list):
            self.top_three = lines
        elif isinstance(lines, dict):
            self.top_three = merge(self.top_three, lines)

    def _on_racecontrolmessages(self, data: dict[str, Any]) -> None:
        self.race_control = merge(self.race_control, data.get("Messages") or [])
        self.race_control = self.race_control[-RACE_CONTROL_KEPT:]

    def _on_teamradio(self, data: dict[str, Any]) -> None:
        self.radio = merge(self.radio, data.get("Captures") or [])
        self.radio = self.radio[-RADIO_KEPT:]

    def _on_sessiondata(self, data: dict[str, Any]) -> None:
        self.session_data = merge(self.session_data, data)

    def _on_cardata_z(self, payload: Any) -> None:
        data = decode_compressed(payload) if isinstance(payload, str) else payload
        for entry in data.get("Entries", []):
            utc = entry.get("Utc")
            for number, car in (entry.get("Cars") or {}).items():
                channels = car.get("Channels") or {}
                sample = {
                    "utc": utc,
                    "speed": _int(channels.get(_CHANNEL_SPEED)),
                    "rpm": _int(channels.get(_CHANNEL_RPM)),
                    "gear": _int(channels.get(_CHANNEL_GEAR)),
                    "throttle": _int(channels.get(_CHANNEL_THROTTLE)),
                    "brake": (_int(channels.get(_CHANNEL_BRAKE)) or 0) > 0,
                    "drs": _int(channels.get(_CHANNEL_DRS)),
                }
                number = str(number)
                self.car[number] = sample
                self.car_history.setdefault(number, deque(maxlen=CAR_HISTORY)).append(sample)

    def _on_position_z(self, payload: Any) -> None:
        data = decode_compressed(payload) if isinstance(payload, str) else payload
        for entry in data.get("Position", []):
            stamp = entry.get("Timestamp")
            for number, place in (entry.get("Entries") or {}).items():
                sample = {
                    "utc": stamp,
                    "x": _number(place.get("X")),
                    "y": _number(place.get("Y")),
                    "z": _number(place.get("Z")),
                    "status": place.get("Status"),
                }
                number = str(number)
                self.position[number] = sample
                self.position_history.setdefault(number, deque(maxlen=POSITION_HISTORY)).append(sample)
                if place.get("Status") == "OnTrack":
                    self.outline.add(number, sample["x"], sample["y"])

    def _note_lap(self, number: str) -> None:
        """A driver's lap counter moving on is a lap finished: keep what the board said at that
        moment, which is what a chart of the race is made of."""
        timing = self.timing.get(number, {})
        laps = _int(timing.get("NumberOfLaps"))
        if laps is None:
            return
        if self._lap_seen.get(number) == laps:
            return
        self._lap_seen[number] = laps
        latest = self.car.get(number)
        if latest and latest.get("utc"):
            self.lap_started[number] = latest["utc"]
        if laps <= 0:
            return
        stints = self._stints(number)
        compound = stints[-1].get("compound") if stints else None
        last = timing.get("LastLapTime") or {}
        self.laps.setdefault(number, []).append(
            {
                "lap": laps,
                "time": _value(last),
                "position": _int(timing.get("Position")),
                # As on the tower: a practice or a qualifying gives the best lap's time behind the
                # fastest and behind the one above instead of the race's gaps on the road.
                "gap_to_leader": _value(timing.get("GapToLeader")) or _value(timing.get("TimeDiffToFastest")),
                "interval": _value(timing.get("IntervalToPositionAhead")) or _value(timing.get("TimeDifftoPositionAhead")),
                "compound": compound,
                "in_pit": bool(timing.get("InPit")),
            }
        )

    # ----------------------------------------------------------------- output

    def _stints(self, number: str) -> list[dict[str, Any]]:
        stints = (self.app.get(number) or {}).get("Stints") or []
        if isinstance(stints, dict):
            stints = [stints[key] for key in sorted(stints, key=lambda item: int(item))]
        out = []
        for stint in stints:
            if not isinstance(stint, dict):
                continue
            out.append(
                {
                    "compound": stint.get("Compound"),
                    "new": str(stint.get("New", "")).lower() == "true",
                    "laps": _int(stint.get("TotalLaps")),
                    "start_lap": _int(stint.get("StartLaps")),
                    "last_lap_time": stint.get("LapTime"),
                }
            )
        return out

    @staticmethod
    def _sectors(timing: dict[str, Any]) -> list[dict[str, Any]]:
        sectors = timing.get("Sectors") or []
        if isinstance(sectors, dict):
            sectors = [sectors[key] for key in sorted(sectors, key=lambda item: int(item))]
        out = []
        for sector in sectors:
            segments = sector.get("Segments") or []
            if isinstance(segments, dict):
                segments = [segments[key] for key in sorted(segments, key=lambda item: int(item))]
            out.append(
                {
                    "value": _value(sector),
                    "previous": sector.get("PreviousValue") or None,
                    "personal_fastest": bool(sector.get("PersonalFastest")),
                    "overall_fastest": bool(sector.get("OverallFastest")),
                    "segments": [
                        SEGMENT_COLOR.get(_int((segment or {}).get("Status")) or 0, "none") for segment in segments
                    ],
                }
            )
        return out

    def _driver(self, number: str) -> dict[str, Any]:
        info = self.drivers.get(number, {})
        timing = self.timing.get(number, {})
        stats = self.stats.get(number, {})
        speeds = timing.get("Speeds") or {}
        stints = self._stints(number)
        best = timing.get("BestLapTime") or {}
        last = timing.get("LastLapTime") or {}
        personal_best = stats.get("PersonalBestLapTime") or {}

        return {
            "number": number,
            "tla": info.get("Tla"),
            "name": " ".join(part for part in (info.get("FirstName"), info.get("LastName")) if part) or None,
            "first_name": info.get("FirstName"),
            "last_name": info.get("LastName"),
            "team": info.get("TeamName"),
            "color": info.get("TeamColour"),
            "headshot": info.get("HeadshotUrl"),
            "position": _int(timing.get("Position")),
            # A race gives the gap to the leader and the interval to the car ahead; a practice or a
            # qualifying gives the time behind the fastest and behind the one above.
            "gap_to_leader": _value(timing.get("GapToLeader")) or _value(timing.get("TimeDiffToFastest")),
            "interval": _value(timing.get("IntervalToPositionAhead")) or _value(timing.get("TimeDifftoPositionAhead")),
            "catching": bool((timing.get("IntervalToPositionAhead") or {}).get("Catching")),
            "knocked_out": bool(timing.get("KnockedOut")),
            "cutoff": bool(timing.get("Cutoff")),
            "last_lap": {
                "value": _value(last),
                "overall_fastest": bool(last.get("OverallFastest")) if isinstance(last, dict) else False,
                "personal_fastest": bool(last.get("PersonalFastest")) if isinstance(last, dict) else False,
            },
            "best_lap": {
                "value": _value(best) or _value(personal_best),
                "lap": _int(best.get("Lap")) if isinstance(best, dict) else None,
            },
            "laps": _int(timing.get("NumberOfLaps")),
            "pit_stops": _int(timing.get("NumberOfPitStops")),
            "in_pit": bool(timing.get("InPit")),
            "pit_out": bool(timing.get("PitOut")),
            "retired": bool(timing.get("Retired")),
            "stopped": bool(timing.get("Stopped")),
            "status": _int(timing.get("Status")),
            "sectors": self._sectors(timing),
            "speeds": {key: _int(_value(speeds.get(key))) for key in ("I1", "I2", "FL", "ST")},
            "best_sectors": [_value(item) for item in (stats.get("BestSectors") or [])]
            if isinstance(stats.get("BestSectors"), list)
            else [],
            "best_speeds": {
                key: {"value": _int(_value(node)), "rank": _int((node or {}).get("Position"))}
                for key, node in (stats.get("BestSpeeds") or {}).items()
            },
            "tyre": stints[-1] if stints else None,
            "stints": stints,
            "car": self.car.get(number),
            "location": self.position.get(number),
        }

    def snapshot(self) -> dict[str, Any]:
        """What a live screen draws, in one piece. No histories: they have their own reads."""
        drivers = [self._driver(number) for number in self._numbers()]
        drivers.sort(key=lambda item: (item["position"] is None, item["position"] or 0, item["number"]))

        flags = [item for item in (self._race_control_items()) if item.get("category") in ("Flag", "SafetyCar")]
        return {
            "session": self._session(),
            "lap": {"current": _int(self.lap_count.get("CurrentLap")), "total": _int(self.lap_count.get("TotalLaps"))},
            "clock": {
                "remaining": self.clock.get("Remaining"),
                "extrapolating": bool(self.clock.get("Extrapolating")),
                "utc": self.clock.get("Utc"),
            },
            "track_status": {
                "code": self.track_status.get("Status"),
                "state": TRACK_STATUS.get(str(self.track_status.get("Status")), "unknown"),
                "message": self.track_status.get("Message"),
            },
            "weather": {
                "air_temp": _number(self.weather.get("AirTemp")),
                "track_temp": _number(self.weather.get("TrackTemp")),
                "humidity": _number(self.weather.get("Humidity")),
                "pressure": _number(self.weather.get("Pressure")),
                "wind_speed": _number(self.weather.get("WindSpeed")),
                "wind_direction": _number(self.weather.get("WindDirection")),
                "rainfall": bool(_number(self.weather.get("Rainfall"))),
            },
            "drivers": drivers,
            "race_control": self._race_control_items()[-40:][::-1],
            "latest_flags": flags[-6:][::-1],
            "radio": self._radio_items()[-15:][::-1],
            "has_car_data": bool(self.car),
            "has_positions": bool(self.position),
            "updated_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "messages": self.messages,
        }

    def _numbers(self) -> list[str]:
        return sorted(set(self.drivers) | set(self.timing))

    def _session(self) -> dict[str, Any]:
        info = self.session_info
        meeting = info.get("Meeting") or {}
        return {
            "meeting": meeting.get("Name"),
            "official_name": meeting.get("OfficialName"),
            "location": meeting.get("Location"),
            "country": (meeting.get("Country") or {}).get("Name"),
            "round": meeting.get("Number"),
            "name": info.get("Name"),
            "type": info.get("Type"),
            "status": self.session_status or info.get("SessionStatus"),
            "start": info.get("StartDate"),
            "end": info.get("EndDate"),
            "gmt_offset": info.get("GmtOffset"),
            "path": info.get("Path"),
        }

    def _race_control_items(self) -> list[dict[str, Any]]:
        items = []
        for message in self.race_control:
            # An index past the end of the list leaves empty items behind it until they are filled.
            if not isinstance(message, dict) or not message.get("Message"):
                continue
            items.append(
                {
                    "utc": message.get("Utc"),
                    "lap": message.get("Lap"),
                    "category": message.get("Category"),
                    "flag": message.get("Flag"),
                    "scope": message.get("Scope"),
                    "sector": message.get("Sector"),
                    "driver": message.get("RacingNumber"),
                    "message": message.get("Message"),
                }
            )
        items.sort(key=lambda item: item.get("utc") or "")
        return items

    def _radio_items(self) -> list[dict[str, Any]]:
        items = []
        for capture in self.radio:
            if not isinstance(capture, dict) or not capture.get("Path"):
                continue
            number = str(capture.get("RacingNumber"))
            items.append(
                {
                    "utc": capture.get("Utc"),
                    "driver": number,
                    "tla": (self.drivers.get(number) or {}).get("Tla"),
                    "path": capture.get("Path"),
                }
            )
        items.sort(key=lambda item: item.get("utc") or "")
        return items

    def circuit_key(self) -> str | None:
        circuit = (self.session_info.get("Meeting") or {}).get("Circuit") or {}
        key = circuit.get("Key")
        return None if key is None else str(key)

    def outline_payload(self) -> dict[str, Any]:
        return self.outline.payload(self.circuit_key())

    def telemetry(self, number: str, seconds: int = 60, lap: bool = False) -> dict[str, Any]:
        """The recent car data of one driver, oldest first, for a trace; or, with lap, all of it
        since the driver's current lap began."""
        history = list(self.car_history.get(str(number), []))
        started = self.lap_started.get(str(number)) if lap else None
        if lap and started:
            begun = _parse_utc(started)
            if begun is not None:
                history = [sample for sample in history if (_parse_utc(sample["utc"]) or begun) >= begun]
            return {"driver": str(number), "samples": history, "lap_started": started}
        if history and seconds > 0:
            last = _parse_utc(history[-1]["utc"])
            if last is not None:
                kept = []
                for sample in history:
                    moment = _parse_utc(sample["utc"])
                    if moment is None or (last - moment).total_seconds() <= seconds:
                        kept.append(sample)
                history = kept
        return {"driver": str(number), "samples": history}

    def trails(self, seconds: int = 30) -> dict[str, list[dict[str, Any]]]:
        """Where each car has been lately, for the tails behind them on the map."""
        out: dict[str, list[dict[str, Any]]] = {}
        for number, history in self.position_history.items():
            samples = list(history)
            if samples and seconds > 0:
                last = _parse_utc(samples[-1]["utc"])
                if last is not None:
                    since = last - timedelta(seconds=seconds)
                    samples = [sample for sample in samples if (_parse_utc(sample["utc"]) or last) >= since]
            out[number] = [{"x": sample["x"], "y": sample["y"]} for sample in samples if sample["x"] is not None]
        return out

    def history(self) -> dict[str, Any]:
        """Lap by lap, per driver: what a chart of the race is drawn from."""
        drivers = []
        for number in self._numbers():
            info = self.drivers.get(number, {})
            drivers.append(
                {
                    "number": number,
                    "tla": info.get("Tla"),
                    "color": info.get("TeamColour"),
                    "laps": self.laps.get(number, []),
                }
            )
        return {"drivers": drivers}
