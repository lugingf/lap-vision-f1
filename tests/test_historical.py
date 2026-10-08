import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pandas as pd  # type: ignore

from app.domain.models import SessionRequest
from app.services.historical import (
    _pick_reference_track_polyline,
    _schedule_session_start,
    _session_bundle_has_data,
    _session_to_bundle_payload,
    _utc_iso,
    fetch_schedule_payload,
)


class SessionBundleHasDataTests(unittest.TestCase):
    """A session bundle with nothing in it must never be cached as if it were the real answer -
    see load_session, which skips the cache write (and distrusts an existing cache entry) when
    this returns False. The incident this guards against: FastF1's @soft_exceptions decorator
    swallows a failed sub-loader (including hitting its own rate limiter) and returns an empty
    result instead of raising, so nothing upstream of this function can tell the difference
    between "genuinely nothing yet" and "silently failed" without it."""

    def test_empty_payload_has_no_data(self) -> None:
        self.assertFalse(_session_bundle_has_data({"drivers": [], "laps": [], "results": []}))

    def test_missing_keys_have_no_data(self) -> None:
        self.assertFalse(_session_bundle_has_data({}))

    def test_drivers_alone_counts_as_data(self) -> None:
        self.assertTrue(_session_bundle_has_data({"drivers": [{"driver_code": "VER"}], "laps": [], "results": []}))

    def test_laps_alone_counts_as_data(self) -> None:
        self.assertTrue(_session_bundle_has_data({"drivers": [], "laps": [{"lap_number": 1}], "results": []}))

    def test_results_alone_counts_as_data(self) -> None:
        self.assertTrue(_session_bundle_has_data({"drivers": [], "laps": [], "results": [{"position": 1}]}))


class ScheduleTimesTests(unittest.TestCase):
    """The official start of a session, always in UTC.

    FastF1 gives Session{N}DateUtc as a naive datetime that is already UTC and Session{N}Date as a
    local time with an offset. Mixing the two up moves a session by the circuit's offset - hours
    from where it really is - so each reading is pinned here."""

    def test_a_naive_datetime_is_read_as_utc(self) -> None:
        self.assertEqual(_utc_iso(pd.Timestamp("2026-10-02 08:00:00")), "2026-10-02T08:00:00Z")

    def test_an_aware_datetime_is_converted_to_utc(self) -> None:
        local = datetime(2026, 10, 2, 16, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(_utc_iso(local), "2026-10-02T08:00:00Z")

    def test_a_date_that_crosses_midnight_in_utc_changes_day(self) -> None:
        local = datetime(2026, 10, 3, 2, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(_utc_iso(local), "2026-10-02T18:00:00Z")

    def test_a_missing_moment_is_none(self) -> None:
        self.assertIsNone(_utc_iso(None))
        self.assertIsNone(_utc_iso(pd.NaT))

    def test_the_utc_column_wins_over_the_local_one(self) -> None:
        row = {
            "Session2DateUtc": pd.Timestamp("2026-10-02 08:00:00"),
            "Session2Date": pd.Timestamp("2026-10-02 16:00:00", tz="Asia/Kuala_Lumpur"),
        }
        self.assertEqual(_schedule_session_start(row, "2"), "2026-10-02T08:00:00Z")

    def test_a_local_time_with_an_offset_is_used_when_there_is_no_utc_column(self) -> None:
        row = {"Session2Date": pd.Timestamp("2026-10-02 16:00:00", tz="Asia/Kuala_Lumpur")}
        self.assertEqual(_schedule_session_start(row, "2"), "2026-10-02T08:00:00Z")

    def test_a_local_time_without_an_offset_is_not_guessed_at(self) -> None:
        row = {"Session2Date": pd.Timestamp("2026-10-02 16:00:00")}
        self.assertIsNone(_schedule_session_start(row, "2"))


class FetchSchedulePayloadTests(unittest.TestCase):
    def _payload(self, frame: pd.DataFrame) -> dict:
        fastf1 = mock.Mock()
        fastf1.get_event_schedule.return_value = frame
        with mock.patch("app.services.historical._import_fastf1", return_value=fastf1):
            return fetch_schedule_payload(2026, "/tmp/unused")

    def test_every_named_session_carries_its_start_in_utc(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "RoundNumber": 16,
                    "EventName": "Bahrain Grand Prix",
                    "EventFormat": "conventional",
                    "EventDate": pd.Timestamp("2026-10-04"),
                    "Session1": "Practice 1",
                    "Session1Date": pd.Timestamp("2026-10-02 12:30:00", tz="Asia/Kuala_Lumpur"),
                    "Session1DateUtc": pd.Timestamp("2026-10-02 04:30:00"),
                    "Session2": "Practice 2",
                    "Session2Date": pd.Timestamp("2026-10-02 16:00:00", tz="Asia/Kuala_Lumpur"),
                    "Session2DateUtc": pd.Timestamp("2026-10-02 08:00:00"),
                    "Session3": None,
                    "Session3Date": pd.NaT,
                    "Session3DateUtc": pd.NaT,
                }
            ]
        )

        event = self._payload(frame)["events"][0]

        self.assertEqual(event["session_names"], ["Practice 1", "Practice 2"])
        self.assertEqual(
            event["sessions"],
            [
                {"session_name": "Practice 1", "starts_at": "2026-10-02T04:30:00Z"},
                {"session_name": "Practice 2", "starts_at": "2026-10-02T08:00:00Z"},
            ],
        )

    def test_a_session_without_a_time_is_still_listed(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "RoundNumber": 1,
                    "EventName": "Australian Grand Prix",
                    "EventDate": pd.Timestamp("2026-03-08"),
                    "Session1": "Practice 1",
                    "Session1DateUtc": pd.NaT,
                }
            ]
        )

        event = self._payload(frame)["events"][0]

        self.assertEqual(event["sessions"], [{"session_name": "Practice 1", "starts_at": None}])


class ScheduleResponseKeepsSessionTimesTest(unittest.TestCase):
    """The schedule is read with the start of each session, and the API must say it too: a model
    that does not name the field drops it, and the callers see only session names."""

    def test_the_response_carries_the_start_of_each_session(self) -> None:
        from app.domain.models import ScheduleResponse

        response = ScheduleResponse.model_validate(
            {
                "season_year": 2026,
                "events": [
                    {
                        "season_year": 2026,
                        "round_number": 16,
                        "event_name": "Bahrain Grand Prix",
                        "session_names": ["Practice 1", "Race"],
                        "sessions": [
                            {"session_name": "Practice 1", "starts_at": "2026-10-02T04:30:00Z"},
                            {"session_name": "Race", "starts_at": None},
                        ],
                    }
                ],
                "cache_hit": True,
                "cache_key": "schedule/2026.json",
            }
        )

        dumped = response.model_dump()["events"][0]["sessions"]
        self.assertEqual(dumped[0], {"session_name": "Practice 1", "starts_at": "2026-10-02T04:30:00Z"})
        self.assertIsNone(dumped[1]["starts_at"])


if __name__ == "__main__":
    unittest.main()


class LapPitFlagsTests(unittest.TestCase):
    """FastF1 marks a lap without a pit visit with NaT, and NaT is not None: the flags used to read
    True on every lap of every session, so nothing downstream could tell a pit lap from any other."""

    def _laps(self) -> list[dict]:
        frame = pd.DataFrame(
            {
                "Driver": ["ANT", "ANT", "ANT", "ANT"],
                "LapNumber": [21, 22, 23, 24],
                "Stint": [1, 1, 2, 2],
                "LapTime": [timedelta(seconds=90)] * 4,
                "PitInTime": [pd.NaT, timedelta(seconds=2010), pd.NaT, pd.NaT],
                "PitOutTime": [pd.NaT, pd.NaT, timedelta(seconds=2032), pd.NaT],
            }
        )
        session = mock.Mock(spec=["laps"])
        session.laps = frame
        request = SessionRequest(year=2026, event=3, session="R", include_weather=False, include_messages=False)
        return _session_to_bundle_payload(session, request)["laps"]

    def test_only_the_in_lap_and_the_out_lap_are_flagged(self) -> None:
        laps = self._laps()
        self.assertEqual([lap["is_pit_in_lap"] for lap in laps], [False, True, False, False])
        self.assertEqual([lap["is_pit_out_lap"] for lap in laps], [False, False, True, False])

    def test_the_flags_come_with_the_times(self) -> None:
        laps = self._laps()
        self.assertEqual(laps[1]["pit_in_time_ms"], 2010000)
        self.assertEqual(laps[2]["pit_out_time_ms"], 2032000)
        self.assertIsNone(laps[0]["pit_in_time_ms"])
        self.assertIsNone(laps[0]["pit_out_time_ms"])


class CachedSessionPitFlagsTests(unittest.IsolatedAsyncioTestCase):
    """A session bundle cached before the fix carries both flags on every lap; its pit times are
    right. Served from the cache, it must come out with the flags its times give."""

    async def test_a_cached_bundle_is_served_with_flags_from_its_pit_times(self) -> None:
        from pathlib import Path

        from app.services.historical import HistoricalService

        lap = {"driver_code": "ANT", "lap_time_ms": 90000, "is_pit_in_lap": True, "is_pit_out_lap": True,
               "pit_in_time_ms": None, "pit_out_time_ms": None, "deleted": False}
        cached = {
            "descriptor": {
                "season_year": 2026, "event_name": "Japanese Grand Prix",
                "session_name": "Race", "session_type": "Race",
            },
            "telemetry_available": False,
            "position_data_available": False,
            "drivers": [{"driver_code": "ANT"}],
            "results": [],
            "laps": [
                {**lap, "lap_number": 21},
                {**lap, "lap_number": 22, "pit_in_time_ms": 5935866},
                {**lap, "lap_number": 23, "pit_out_time_ms": 5959138},
            ],
            "stints": [],
            "weather": [],
            "race_control": [],
        }

        class _Cache:
            def read_json(self, _key: str):
                return dict(cached, laps=[dict(item) for item in cached["laps"]])

            def write_json(self, _key: str, _payload) -> None:
                raise AssertionError("a cache hit must not be written again")

        with mock.patch("app.services.historical.ProcessPoolExecutor"):
            service = HistoricalService(Path("/tmp/fastf1"), _Cache(), worker_processes=1)
        no_compute = AssertionError("a cache hit must not be computed")
        with mock.patch.object(service, "_compute_once", side_effect=no_compute):
            bundle = await service.load_session(SessionRequest(year=2026, event=3, session="R"))

        self.assertTrue(bundle.cache_hit)
        self.assertEqual([lap.is_pit_in_lap for lap in bundle.laps], [False, True, False])
        self.assertEqual([lap.is_pit_out_lap for lap in bundle.laps], [False, False, True])
        self.assertEqual(bundle.laps[1].pit_in_time_ms, 5935866)

    async def test_a_bundle_cached_before_laps_carried_pit_times_is_loaded_again(self) -> None:
        from pathlib import Path

        from app.services.historical import HistoricalService

        stale_lap = {"driver_code": "ANT", "lap_number": 22, "lap_time_ms": 90000,
                     "is_pit_in_lap": True, "is_pit_out_lap": True, "deleted": False}
        base = {
            "descriptor": {"season_year": 2026, "event_name": "Japanese Grand Prix", "session_name": "Race",
                           "session_type": "Race"},
            "telemetry_available": False,
            "position_data_available": False,
            "drivers": [{"driver_code": "ANT"}],
            "results": [],
            "stints": [],
            "weather": [],
            "race_control": [],
        }
        fresh_lap = {**stale_lap, "is_pit_out_lap": False, "pit_in_time_ms": 5935866, "pit_out_time_ms": None}
        written: list[dict] = []

        class _Cache:
            def read_json(self, _key: str):
                return {**base, "laps": [dict(stale_lap)]}

            def write_json(self, _key: str, payload) -> None:
                written.append(payload)

        async def compute(*_args):
            return {**base, "laps": [dict(fresh_lap)]}

        with mock.patch("app.services.historical.ProcessPoolExecutor"):
            service = HistoricalService(Path("/tmp/fastf1"), _Cache(), worker_processes=1)
        with mock.patch.object(service, "_compute_once", side_effect=compute):
            bundle = await service.load_session(SessionRequest(year=2026, event=3, session="R"))

        self.assertFalse(bundle.cache_hit)
        self.assertEqual(len(written), 1, "the reloaded session replaces the stale cache entry")
        self.assertTrue(bundle.laps[0].is_pit_in_lap)
        self.assertFalse(bundle.laps[0].is_pit_out_lap)


class ReferenceTrackPolylineTests(unittest.TestCase):
    def test_outline_is_the_quickest_clean_lap(self):
        from fastf1.core import Lap, Laps  # type: ignore

        laps = Laps(
            pd.DataFrame(
                {
                    "Driver": ["AAA", "BBB", "CCC"],
                    "LapNumber": [1, 2, 3],
                    "LapTime": [pd.NaT, pd.Timedelta(seconds=88), pd.Timedelta(seconds=90)],
                    "Deleted": [False, True, False],
                }
            )
        )

        def pos_data(lap):
            offset = {"AAA": 0.0, "BBB": 1000.0, "CCC": 2000.0}[lap["Driver"]]
            return pd.DataFrame(
                {"X": [offset + i for i in range(50)], "Y": [float(i) for i in range(50)], "Status": ["OnTrack"] * 50}
            )

        session = mock.Mock(spec=[])
        with mock.patch.object(Lap, "get_pos_data", pos_data):
            points, source = _pick_reference_track_polyline(session, laps)

        self.assertEqual(source, "reference_lap")
        self.assertEqual(len(points), 50)
        self.assertEqual(points[0], {"x": 2000.0, "y": 0.0})
