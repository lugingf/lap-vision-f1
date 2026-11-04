import unittest
from unittest import mock

import pandas as pd  # type: ignore

from app.services import historical
from app.services.historical import (
    _bin_centres,
    _clean_race_laps,
    _corners_from_speed,
    _lap_elapsed,
    _lap_gear_on_bins,
    _lap_on_bins,
    _median_trace,
)


class LapOnBinsTests(unittest.TestCase):
    def test_a_lap_is_measured_against_its_own_length(self) -> None:
        centres = _bin_centres(4)
        short = _lap_on_bins([0.0 + step for step in range(0, 1000, 10)], {"speed": range(100)}, centres)
        longer = _lap_on_bins([0.0 + step * 1.1 for step in range(0, 1000, 10)], {"speed": range(100)}, centres)

        assert short is not None and longer is not None
        # The same point of two laps of different length lands in the same place.
        self.assertEqual([round(value, 3) for value in short["speed"]], [round(value, 3) for value in longer["speed"]])

    def test_a_lap_with_too_few_samples_says_nothing(self) -> None:
        self.assertIsNone(_lap_on_bins([0.0, 10.0, 20.0], {"speed": [1, 2, 3]}, _bin_centres(4)))


class MedianTraceTests(unittest.TestCase):
    def test_the_typical_lap_is_the_median_and_the_brake_a_share(self) -> None:
        laps = [
            {
                "speed": [100.0, 200.0],
                "throttle": [0.0, 100.0],
                "brake": [1.0, 0.0],
                "gear": [3, 7],
                "elapsed": [0.0, 10.0, 15.0],
            },
            {
                "speed": [110.0, 210.0],
                "throttle": [10.0, 100.0],
                "brake": [1.0, 0.0],
                "gear": [3, 8],
                "elapsed": [0.0, 9.0, 14.0],
            },
            {
                "speed": [300.0, 205.0],
                "throttle": [100.0, 99.0],
                "brake": [0.0, 0.0],
                "gear": [8, 6],
                "elapsed": [0.0, 5.0, 14.5],
            },
        ]
        trace = _median_trace(laps)

        # One lap that went through the corner flat does not drag the median with it.
        self.assertEqual(trace["speed"], [110.0, 205.0])
        self.assertEqual(trace["throttle"], [10.0, 100.0])
        self.assertEqual(trace["brake"], [0.667, 0.0])
        # The gear is the one most laps were in; three gears once each give the middle one.
        self.assertEqual(trace["gear"], [3, 7])
        # The time of each point is read between the median times its edges are reached, so the
        # points add up to the median lap.
        self.assertEqual(trace["time"], [9.0, 5.5])


class LapGearOnBinsTests(unittest.TestCase):
    def test_a_point_takes_the_gear_of_the_nearest_sample(self) -> None:
        distance = [float(step) for step in range(100)]
        gear = [3] * 50 + [4] * 50

        on_bins = _lap_gear_on_bins(distance, gear, _bin_centres(4))

        assert on_bins is not None
        # Never 3.5 between the two: a gear is one or the other.
        self.assertEqual([int(value) for value in on_bins], [3, 3, 4, 4])


class LapElapsedTests(unittest.TestCase):
    def test_the_clock_not_the_speed_says_how_long_a_stretch_took(self) -> None:
        distance = [float(step) for step in range(100)]
        clock = pd.Series(pd.to_timedelta([100.0 + step * 0.5 for step in range(100)], unit="s"))

        elapsed = _lap_elapsed(distance, clock, 99.0, 2)

        assert elapsed is not None
        # The samples span 49.5 s of a lap timed at 99 s: the clock is stretched to the timed lap.
        self.assertEqual([round(float(value), 3) for value in elapsed], [0.0, 49.5, 99.0])

    def test_a_lap_with_too_few_samples_says_nothing(self) -> None:
        clock = pd.Series(pd.to_timedelta([0.0, 1.0, 2.0], unit="s"))
        self.assertIsNone(_lap_elapsed([0.0, 10.0, 20.0], clock, 3.0, 2))


class CleanRaceLapsTests(unittest.TestCase):
    def test_only_laps_that_measure_pace_are_kept(self) -> None:
        second = pd.Timedelta(seconds=1)
        laps = pd.DataFrame(
            {
                "LapNumber": [1, 2, 3, 4, 5, 6, 7, 8],
                "LapTime": [
                    95 * second,
                    80 * second,
                    81 * second,
                    99 * second,
                    80 * second,
                    80 * second,
                    80 * second,
                    None,
                ],
                "PitInTime": [pd.NaT, pd.NaT, pd.NaT, pd.NaT, 5 * second, pd.NaT, pd.NaT, pd.NaT],
                "PitOutTime": [pd.NaT] * 8,
                "TrackStatus": ["1", "1", "1", "1", "1", "14", "1", "1"],
                "Deleted": [False, False, None, False, False, False, True, False],
            }
        )

        kept = _clean_race_laps(laps)

        # Not the first, not the in-lap, not under a yellow, not deleted, timed, and lap 4 is 99 s
        # against a median of 80.
        self.assertEqual(list(kept["LapNumber"]), [2, 3])


class CornersFromSpeedTests(unittest.TestCase):
    def test_the_slow_points_of_a_lap_are_its_corners_in_order(self) -> None:
        speed = [300.0] * 100
        for index, value in ((20, 90.0), (21, 95.0), (60, 150.0), (61, 160.0), (80, 290.0)):
            speed[index] = value

        corners = _corners_from_speed(speed)

        # A lift of 10 km/h on a straight is no corner.
        self.assertEqual([corner["number"] for corner in corners], [1, 2])
        self.assertEqual([corner["fraction"] for corner in corners], [0.205, 0.605])


class OfflineTests(unittest.TestCase):
    def test_a_race_that_is_not_cached_is_unavailable_and_offline_mode_is_left_off(self) -> None:
        import fastf1

        def missing(*args: object) -> object:
            raise ValueError("not cached")

        modes: list[bool] = []
        with (
            mock.patch.object(historical, "_import_fastf1", lambda cache_dir: fastf1),
            mock.patch.object(fastf1, "get_session", missing),
            mock.patch.object(fastf1.Cache, "offline_mode", lambda enabled: modes.append(enabled)),
        ):
            payload = historical.fetch_race_traces_payload({"year": 2026, "event": 2, "session": "Race"}, "unused")

        self.assertFalse(payload["available"])
        self.assertIn("not in the cache", payload["message"])
        self.assertEqual(modes, [True, False])


if __name__ == "__main__":
    unittest.main()
