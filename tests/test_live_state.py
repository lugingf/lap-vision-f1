import ast
import base64
import json
import math
import unittest
import zlib
from pathlib import Path

from app.services.live_state import LiveState, decode_compressed, merge

FIXTURE = Path(__file__).parent / "fixtures" / "live_race_sample.txt"


def recorded() -> LiveState:
    """A 45-second stretch of the Bahrain race, as the recorder wrote it: the first message of each
    topic is the whole object, the rest are changes."""
    state = LiveState()
    for line in FIXTURE.read_text().splitlines():
        topic, payload, stamp = ast.literal_eval(line)
        state.feed(topic, payload, stamp)
    return state


def compress(payload: dict) -> str:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = compressor.compress(json.dumps(payload).encode()) + compressor.flush()
    return base64.b64encode(raw).decode()


class MergeTests(unittest.TestCase):
    def test_a_dict_changes_a_dict_key_by_key(self) -> None:
        self.assertEqual(merge({"a": 1, "b": {"c": 2}}, {"b": {"d": 3}}), {"a": 1, "b": {"c": 2, "d": 3}})

    def test_a_dict_keyed_by_index_changes_the_items_of_a_list(self) -> None:
        target = [{"Value": "1"}, {"Value": "2"}, {"Value": "3"}]
        self.assertEqual(merge(target, {"1": {"Value": "9"}}), [{"Value": "1"}, {"Value": "9"}, {"Value": "3"}])

    def test_an_index_past_the_end_extends_the_list(self) -> None:
        self.assertEqual(merge([{"a": 1}], {"2": {"a": 3}}), [{"a": 1}, {}, {"a": 3}])

    def test_a_list_replaces_what_was_there(self) -> None:
        self.assertEqual(merge({"x": [1, 2]}, {"x": [3]}), {"x": [3]})

    def test_the_feeds_own_marker_is_dropped(self) -> None:
        self.assertEqual(merge({}, {"a": 1, "_kf": True}), {"a": 1})


class RecordedRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = recorded()
        cls.snapshot = cls.state.snapshot()

    def test_the_session_is_named(self) -> None:
        session = self.snapshot["session"]
        self.assertEqual(session["meeting"], "Bahrain Grand Prix")
        self.assertEqual(session["name"], "Race")
        self.assertEqual(session["status"], "Started")

    def test_the_lap_counter_and_the_clock_are_read(self) -> None:
        self.assertEqual(self.snapshot["lap"], {"current": 13, "total": 55})
        self.assertEqual(self.snapshot["clock"]["remaining"], "01:32:11")

    def test_every_driver_is_on_the_board_in_order(self) -> None:
        drivers = self.snapshot["drivers"]
        self.assertEqual(len(drivers), 22)
        positions = [driver["position"] for driver in drivers if driver["position"] is not None]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(drivers[0]["position"], 1)

    def test_a_driver_carries_what_the_tower_shows(self) -> None:
        leader = self.snapshot["drivers"][0]
        for key in (
            "tla",
            "name",
            "team",
            "color",
            "gap_to_leader",
            "interval",
            "last_lap",
            "best_lap",
            "laps",
            "sectors",
            "speeds",
            "tyre",
        ):
            self.assertIn(key, leader)
        self.assertTrue(leader["tla"])
        self.assertEqual(len(leader["sectors"]), 3)
        self.assertIsNotNone(leader["tyre"]["compound"])

    def test_gaps_follow_the_order(self) -> None:
        second = self.snapshot["drivers"][1]
        self.assertTrue(second["gap_to_leader"].startswith("+"))

    def test_mini_sectors_are_named_by_colour(self) -> None:
        colours = {
            segment
            for driver in self.snapshot["drivers"]
            for sector in driver["sectors"]
            for segment in sector["segments"]
        }
        self.assertTrue(colours <= {"none", "yellow", "green", "purple", "pit"})
        self.assertIn("green", colours)

    def test_race_control_is_newest_first(self) -> None:
        feed = self.snapshot["race_control"]
        self.assertGreater(len(feed), 3)
        stamps = [item["utc"] for item in feed]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_the_track_status_and_weather_are_read(self) -> None:
        self.assertIn(self.snapshot["track_status"]["state"], {"clear", "yellow"})
        self.assertEqual(self.snapshot["weather"]["track_temp"], 33.7)

    def test_team_radio_names_the_driver(self) -> None:
        radio = self.snapshot["radio"]
        self.assertTrue(radio)
        self.assertTrue(all(item["tla"] for item in radio))

    def test_no_car_data_arrives_without_a_subscription(self) -> None:
        self.assertFalse(self.snapshot["has_car_data"])
        self.assertFalse(self.snapshot["has_positions"])

    def test_a_lap_finished_is_remembered_for_the_charts(self) -> None:
        history = self.state.history()["drivers"]
        self.assertTrue(any(driver["laps"] for driver in history))


class CompressedTopicTests(unittest.TestCase):
    def test_car_data_is_decoded_into_a_trace(self) -> None:
        state = LiveState()
        for second in range(5):
            state.feed(
                "CarData.z",
                compress(
                    {
                        "Entries": [
                            {
                                "Utc": f"2026-10-04T09:00:0{second}.000Z",
                                "Cars": {
                                    "3": {
                                        "Channels": {"0": 11000, "2": 280 + second, "3": 7, "4": 100, "5": 0, "45": 0}
                                    }
                                },
                            }
                        ]
                    }
                ),
            )
        trace = state.telemetry("3", seconds=60)["samples"]
        self.assertEqual([sample["speed"] for sample in trace], [280, 281, 282, 283, 284])
        self.assertEqual(trace[0]["gear"], 7)
        self.assertFalse(trace[0]["brake"])
        self.assertEqual(state.snapshot()["has_car_data"], True)

    def test_a_trace_is_cut_to_the_seconds_asked_for(self) -> None:
        state = LiveState()
        for second in range(30):
            state.feed(
                "CarData.z",
                compress(
                    {
                        "Entries": [
                            {"Utc": f"2026-10-04T09:00:{second:02d}.000Z", "Cars": {"3": {"Channels": {"2": second}}}}
                        ]
                    }
                ),
            )
        trace = state.telemetry("3", seconds=10)["samples"]
        self.assertEqual(trace[0]["speed"], 19)
        self.assertEqual(len(trace), 11)

    def test_a_brake_is_on_when_the_channel_says_so(self) -> None:
        state = LiveState()
        state.feed(
            "CarData.z",
            compress({"Entries": [{"Utc": "2026-10-04T09:00:00.000Z", "Cars": {"3": {"Channels": {"5": 104}}}}]}),
        )
        self.assertTrue(state.car["3"]["brake"])

    def test_positions_are_decoded_for_the_map(self) -> None:
        state = LiveState()
        state.feed(
            "Position.z",
            compress(
                {
                    "Position": [
                        {
                            "Timestamp": "2026-10-04T09:00:00.100Z",
                            "Entries": {"3": {"Status": "OnTrack", "X": 120, "Y": -45, "Z": 7}},
                        }
                    ]
                }
            ),
        )
        location = state.snapshot()
        state.feed("TimingData", {"Lines": {"3": {"Position": "1"}}})
        driver = state.snapshot()["drivers"][0]
        self.assertEqual(driver["location"]["x"], 120)
        self.assertEqual(driver["location"]["status"], "OnTrack")
        self.assertTrue(location["has_positions"])
        self.assertEqual(state.trails(seconds=30)["3"], [{"x": 120, "y": -45}])

    def test_decode_round_trips(self) -> None:
        self.assertEqual(decode_compressed(compress({"a": [1, 2]})), {"a": [1, 2]})


class QualifyingTests(unittest.TestCase):
    def test_the_gaps_of_a_qualifying_are_read_from_its_own_fields(self) -> None:
        state = LiveState()
        state.feed(
            "TimingData",
            {
                "Lines": {
                    "3": {"Position": "2", "TimeDiffToFastest": "+0.123", "TimeDifftoPositionAhead": "+0.045"},
                    "12": {"Position": "17", "KnockedOut": True},
                }
            },
        )
        by_number = {driver["number"]: driver for driver in state.snapshot()["drivers"]}
        self.assertEqual(by_number["3"]["gap_to_leader"], "+0.123")
        self.assertEqual(by_number["3"]["interval"], "+0.045")
        self.assertTrue(by_number["12"]["knocked_out"])


class RobustnessTests(unittest.TestCase):
    def test_a_message_that_cannot_be_read_is_ignored(self) -> None:
        state = LiveState()
        state.feed("TimingData", "{not json")
        state.feed("CarData.z", "not base64 at all")
        state.feed("Unknown", {"x": 1})
        self.assertEqual(state.snapshot()["drivers"], [])

    def test_a_lap_is_noted_once(self) -> None:
        state = LiveState()
        state.feed(
            "TimingData", {"Lines": {"3": {"NumberOfLaps": 4, "Position": "2", "LastLapTime": {"Value": "1:30.000"}}}}
        )
        state.feed("TimingData", {"Lines": {"3": {"GapToLeader": "+1.2"}}})
        state.feed("TimingData", {"Lines": {"3": {"NumberOfLaps": 5, "LastLapTime": {"Value": "1:29.500"}}}})
        laps = state.history()["drivers"][0]["laps"]
        self.assertEqual([lap["lap"] for lap in laps], [4, 5])
        self.assertEqual(laps[1]["time"], "1:29.500")

    def test_a_qualifying_lap_keeps_its_time_behind_the_fastest(self) -> None:
        state = LiveState()
        state.feed(
            "TimingData",
            {
                "Lines": {
                    "1": {"NumberOfLaps": 3, "Position": "1", "TimeDiffToFastest": "", "TimeDifftoPositionAhead": ""},
                    "3": {"NumberOfLaps": 4, "Position": "2", "TimeDiffToFastest": "+0.123", "TimeDifftoPositionAhead": "+0.123"},
                    "12": {"NumberOfLaps": 5, "Position": "3", "TimeDiffToFastest": "+0.168", "TimeDifftoPositionAhead": "+0.045"},
                }
            },
        )
        laps = {driver["number"]: driver["laps"] for driver in state.history()["drivers"]}
        self.assertEqual((laps["1"][0]["position"], laps["1"][0]["gap_to_leader"]), (1, None))
        self.assertEqual((laps["3"][0]["gap_to_leader"], laps["3"][0]["interval"]), ("+0.123", "+0.123"))
        self.assertEqual((laps["12"][0]["gap_to_leader"], laps["12"][0]["interval"]), ("+0.168", "+0.045"))

    def test_a_practice_lap_keeps_its_time_behind_the_fastest_as_a_value_node(self) -> None:
        state = LiveState()
        state.feed(
            "TimingData",
            {"Lines": {"44": {"NumberOfLaps": 7, "Position": "4", "TimeDiffToFastest": {"Value": "+0.912"}, "TimeDifftoPositionAhead": {"Value": "+0.210"}}}},
        )
        lap = state.history()["drivers"][0]["laps"][0]
        self.assertEqual((lap["gap_to_leader"], lap["interval"]), ("+0.912", "+0.210"))

    def test_a_race_lap_keeps_the_gaps_on_the_road(self) -> None:
        state = LiveState()
        state.feed(
            "TimingData",
            {
                "Lines": {
                    "3": {
                        "NumberOfLaps": 12,
                        "Position": "2",
                        "GapToLeader": "+2.717",
                        "IntervalToPositionAhead": {"Value": "+2.717", "Catching": True},
                        "TimeDiffToFastest": "+0.400",
                    }
                }
            },
        )
        lap = state.history()["drivers"][0]["laps"][0]
        self.assertEqual((lap["gap_to_leader"], lap["interval"]), ("+2.717", "+2.717"))


def _car_message(stamp: str, speed: int) -> dict:
    return {"Entries": [{"Utc": stamp, "Cars": {"3": {"Channels": {"2": speed}}}}]}


def _position_message(stamp: str, x: float, y: float) -> dict:
    return {"Position": [{"Timestamp": stamp, "Entries": {"3": {"Status": "OnTrack", "X": x, "Y": y, "Z": 0}}}]}


class LapTelemetryTests(unittest.TestCase):
    def test_a_lap_starts_where_the_counter_moved_on(self) -> None:
        state = LiveState()
        state.feed("CarData.z", _car_message("2026-10-04T09:00:01Z", 100))
        state.feed("TimingData", {"Lines": {"3": {"NumberOfLaps": 1}}})
        state.feed("CarData.z", _car_message("2026-10-04T09:00:02Z", 200))
        state.feed("CarData.z", _car_message("2026-10-04T09:00:03Z", 300))
        lap = state.telemetry("3", lap=True)
        self.assertEqual([sample["speed"] for sample in lap["samples"]], [100, 200, 300])
        state.feed("TimingData", {"Lines": {"3": {"NumberOfLaps": 2}}})
        state.feed("CarData.z", _car_message("2026-10-04T09:00:04Z", 400))
        lap = state.telemetry("3", lap=True)
        self.assertEqual([sample["speed"] for sample in lap["samples"]], [300, 400])
        self.assertEqual(lap["lap_started"], "2026-10-04T09:00:03Z")


class OutlineTests(unittest.TestCase):
    def test_the_outline_is_drawn_once_round_and_then_kept(self) -> None:
        state = LiveState()
        for _ in range(3):
            for degree in range(360):
                angle = math.radians(degree)
                x, y = math.cos(angle) * 5000, math.sin(angle) * 5000
                state.feed("Position.z", _position_message("2026-10-04T09:00:00Z", x, y))
        outline = state.outline_payload()
        self.assertTrue(outline["closed"])
        self.assertLessEqual(len(outline["polyline"]), 361)
        self.assertEqual(outline["polyline"][0], outline["polyline"][-1])
        self.assertGreater(len(outline["polyline"]), 100)

    def test_a_loaded_outline_is_not_redrawn(self) -> None:
        state = LiveState()
        state.outline.load([{"x": 0, "y": 0}, {"x": 100, "y": 0}])
        state.feed("Position.z", _position_message("2026-10-04T09:00:00Z", 5000, 5000))
        self.assertEqual(len(state.outline_payload()["polyline"]), 2)

    def test_only_one_car_draws_it(self) -> None:
        state = LiveState()
        state.feed("Position.z", _position_message("2026-10-04T09:00:00Z", 0, 0))
        other = {"44": {"Status": "OnTrack", "X": 900, "Y": 900, "Z": 0}}
        state.feed("Position.z", {"Position": [{"Timestamp": "2026-10-04T09:00:01Z", "Entries": other}]})
        self.assertEqual(len(state.outline_payload()["polyline"]), 1)


if __name__ == "__main__":
    unittest.main()
