import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pandas as pd  # type: ignore

from app.services.historical import (
    _schedule_session_start,
    _session_bundle_has_data,
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
