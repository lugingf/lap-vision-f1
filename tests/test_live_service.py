import asyncio
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.models import LiveSessionRequest
from app.services.live import LiveSessionService, _LiveSession

REQUEST = LiveSessionRequest(year=2026, event="16", session="Race")


def _service(directory: Path) -> LiveSessionService:
    return LiveSessionService(SimpleNamespace(get_proxy_urls=lambda: []), directory, 5, None, False)


def _session(directory: Path, recorded: bool) -> _LiveSession:
    process = SimpleNamespace(is_alive=lambda: True) if recorded else None
    return _LiveSession(request=REQUEST, raw_file=directory / "raw.txt", process=process)  # type: ignore[arg-type]


def _circuit(session: _LiveSession) -> None:
    session.state.feed("SessionInfo", {"Meeting": {"Circuit": {"Key": 12}}})


def _drive_a_lap(session: _LiveSession) -> None:
    for _ in range(2):
        for degree in range(360):
            angle = math.radians(degree)
            entries = {"3": {"Status": "OnTrack", "X": math.cos(angle) * 5000, "Y": math.sin(angle) * 5000, "Z": 0}}
            session.state.feed("Position.z", {"Position": [{"Timestamp": "2026-10-04T07:00:00Z", "Entries": entries}]})
    # 360 points of one lap are over the minimum of a lap that is real
    assert session.state.outline.closed


class OutlineKeepingTests(unittest.TestCase):
    def test_a_closed_outline_is_kept_for_the_next_session_at_the_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            service = _service(directory)

            first = _session(directory, recorded=True)
            _circuit(first)
            _drive_a_lap(first)
            service._sync_outline(first)
            self.assertTrue((directory / "outlines" / "12.json").exists())

            second = _session(directory, recorded=True)
            _circuit(second)
            service._sync_outline(second)
            self.assertTrue(second.state.outline.closed)
            self.assertEqual(len(second.state.outline.points), len(first.state.outline.points))

    def test_a_replay_neither_keeps_nor_reads_an_outline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            service = _service(directory)

            replay = _session(directory, recorded=False)
            _circuit(replay)
            _drive_a_lap(replay)
            service._sync_outline(replay)
            self.assertFalse((directory / "outlines").exists())

            (directory / "outlines").mkdir()
            (directory / "outlines" / "12.json").write_text(json.dumps([{"x": 0, "y": 0}, {"x": 1, "y": 1}]))
            other = _session(directory, recorded=False)
            _circuit(other)
            service._sync_outline(other)
            self.assertEqual(other.state.outline.points, [])

    def test_an_outline_file_that_cannot_be_read_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            service = _service(directory)
            (directory / "outlines").mkdir()
            (directory / "outlines" / "12.json").write_text("{broken")
            session = _session(directory, recorded=True)
            _circuit(session)
            service._sync_outline(session)
            self.assertEqual(session.state.outline.points, [])


class TailTests(unittest.TestCase):
    def test_a_service_that_starts_late_reads_the_recording_from_the_top(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            service = _service(directory)
            session = _session(directory, recorded=True)
            lines = [
                repr(["SessionInfo", '{"Meeting": {"Name": "Bahrain"}}', "2026-10-04T07:00:00Z"]),
                repr(["TimingData", '{"Lines": {"3": {"NumberOfLaps": 1, "Position": "1"}}}', "2026-10-04T07:01:00Z"]),
                "not a message",
                repr(["TimingData", '{"Lines": {"3": {"NumberOfLaps": 2}}}', "2026-10-04T07:02:30Z"]),
            ]
            session.raw_file.write_text("\n".join(lines) + "\n")
            service._sessions[service._key(REQUEST)] = session

            async def run() -> None:
                task = asyncio.create_task(service._tail_loop(service._key(REQUEST)))
                await asyncio.sleep(0.8)
                task.cancel()

            asyncio.run(run())
            self.assertEqual(session.state.messages, 3)
            self.assertEqual([lap["lap"] for lap in session.state.history()["drivers"][0]["laps"]], [1, 2])

    def test_a_half_written_line_waits_for_its_end(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            service = _service(directory)
            session = _session(directory, recorded=True)
            whole = repr(["TimingData", '{"Lines": {"3": {"NumberOfLaps": 1}}}', "2026-10-04T07:01:00Z"])
            session.raw_file.write_text(whole[:20])
            service._sessions[service._key(REQUEST)] = session

            async def run() -> None:
                task = asyncio.create_task(service._tail_loop(service._key(REQUEST)))
                await asyncio.sleep(0.8)
                self.assertEqual(session.state.messages, 0)
                with session.raw_file.open("a") as handle:
                    handle.write(whole[20:] + "\n")
                await asyncio.sleep(0.8)
                task.cancel()

            asyncio.run(run())
            self.assertEqual(session.state.messages, 1)


if __name__ == "__main__":
    unittest.main()
