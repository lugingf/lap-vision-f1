import asyncio
import base64
import json
import math
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from app.domain.models import LiveSessionRequest
from app.services.live import LiveSessionService, _LiveSession, _run_recorder_process, valid_token

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


def _jwt(expires_in: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + expires_in}).encode()).decode().rstrip("=")
    return f"header.{claims}.signature"


class StateWaitingTests(unittest.TestCase):
    def test_no_state_until_the_first_message_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            service = _service(directory)
            live = _session(directory, recorded=True)
            service._sessions[service._key(REQUEST)] = live

            self.assertIsNone(service.state(REQUEST))

            _circuit(live)
            payload = service.state(REQUEST)
            self.assertIsNotNone(payload)
            self.assertTrue(payload["running"])  # type: ignore[index]


class RecorderAuthTests(unittest.TestCase):
    def _run(self, token_file: Path | None) -> tuple[dict, str]:
        import fastf1.livetiming.client as livetiming_client

        captured: dict = {}

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            def start(self) -> None:
                captured["token"] = livetiming_client.get_auth_token()

        with (
            mock.patch.object(livetiming_client, "SignalRClient", FakeClient),
            mock.patch.object(livetiming_client, "get_auth_token", livetiming_client.get_auth_token),
        ):
            _run_recorder_process("raw.txt", None, str(token_file) if token_file else None)
        return captured, captured["token"]

    def test_valid_token_decides_by_presence_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "token"
            self.assertEqual(valid_token(path), "")  # missing file
            self.assertEqual(valid_token(None), "")
            path.write_text(_jwt(-60))
            self.assertEqual(valid_token(path), "")  # expired
            good = _jwt(3600)
            path.write_text(good)
            self.assertEqual(valid_token(path), good)

    def test_recorder_is_anonymous_without_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            kwargs, token = self._run(Path(raw) / "missing")
        self.assertTrue(kwargs["no_auth"])
        self.assertEqual(token, "")

    def test_recorder_is_anonymous_with_an_expired_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "token"
            path.write_text(_jwt(-60))
            kwargs, token = self._run(path)
        self.assertTrue(kwargs["no_auth"])
        self.assertEqual(token, "")

    def test_recorder_authenticates_with_a_valid_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "token"
            good = _jwt(3600)
            path.write_text(good)
            kwargs, token = self._run(path)
        self.assertFalse(kwargs["no_auth"])
        self.assertEqual(token, good)


class _FakeSocks5(threading.Thread):
    """A SOCKS5 proxy that records where it is asked to connect and answers as the far end itself."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.targets: list[tuple[str, int]] = []

    def run(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                self._serve(connection)

    def _serve(self, connection: socket.socket) -> None:
        _, methods = connection.recv(2)
        connection.recv(methods)
        connection.sendall(b"\x05\x00")
        _, _, _, kind = connection.recv(4)
        if kind == 3:
            host = connection.recv(connection.recv(1)[0]).decode()
        else:
            host = socket.inet_ntoa(connection.recv(4))
        port = int.from_bytes(connection.recv(2), "big")
        self.targets.append((host, port))
        if (host, port) == ("127.0.0.1", self.port):
            connection.sendall(b"\x05\x06\x00\x01" + bytes(6))  # TTL expired: the proxy asked to reach itself
            return
        connection.sendall(b"\x05\x00\x00\x01" + bytes(6))
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = connection.recv(4096)
            if not chunk:
                return
            request += chunk
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nSet-Cookie: AWSALBCORS=x\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )


class RecorderProxyTests(unittest.TestCase):
    def test_negotiation_goes_through_the_socks_proxy_once(self) -> None:
        import fastf1.livetiming.client as livetiming_client
        import socks

        proxy = _FakeSocks5()
        proxy.start()
        self.addCleanup(proxy.server.close)
        proxy_url = f"socks5h://127.0.0.1:{proxy.port}"
        target = "http://127.0.0.1:8443/signalr/negotiate"
        answers: list[str] = []

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                pass

            def start(self) -> None:
                # FastF1's pre-negotiation, then the SignalR negotiation the way signalrcore opens it.
                answers.append(requests.options(target, timeout=5).text)
                request = urllib.request.Request(target)
                with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=5) as response:
                    answers.append(response.read().decode())

        inherited = {name: proxy_url for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
        with (
            mock.patch.dict(os.environ, inherited),
            mock.patch.object(socket, "socket", socket.socket),
            mock.patch.object(socks.socksocket, "default_proxy", None),
            mock.patch.object(livetiming_client, "SignalRClient", FakeClient),
            mock.patch.object(livetiming_client, "get_auth_token", livetiming_client.get_auth_token),
        ):
            _run_recorder_process("raw.txt", proxy_url, None)

        self.assertEqual(answers, ["ok", "ok"])
        self.assertEqual(proxy.targets, [("127.0.0.1", 8443), ("127.0.0.1", 8443)])


class _FakeProcess:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.terminated = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        pass


class WatchdogTests(unittest.TestCase):
    def _setup(self, directory: Path, token_file: Path | None = None) -> tuple[LiveSessionService, _LiveSession, list]:
        service = LiveSessionService(SimpleNamespace(get_proxy_urls=lambda: []), directory, 5, token_file, False)
        live = _LiveSession(request=REQUEST, raw_file=directory / "raw.txt", process=_FakeProcess())  # type: ignore[arg-type]
        live.last_spawn = time.time() - 60
        spawned: list[_FakeProcess] = []

        def spawn(session: _LiveSession) -> None:
            session.process = _FakeProcess()  # type: ignore[assignment]
            session.token = valid_token(token_file)
            session.last_spawn = time.time()
            spawned.append(session.process)  # type: ignore[arg-type]

        service._spawn = spawn  # type: ignore[method-assign]
        return service, live, spawned

    def test_a_dead_recorder_is_started_again(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service, live, spawned = self._setup(Path(raw))
            live.process.alive = False  # type: ignore[union-attr]
            service._supervise(live)
            self.assertEqual(len(spawned), 1)
            self.assertEqual(live.restarts, 1)

    def test_a_live_recorder_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service, live, spawned = self._setup(Path(raw))
            service._supervise(live)
            self.assertEqual(spawned, [])

    def test_restarts_wait_for_the_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service, live, spawned = self._setup(Path(raw))
            live.process.alive = False  # type: ignore[union-attr]
            live.last_spawn = time.time()
            service._supervise(live)
            self.assertEqual(spawned, [])

    def test_a_replay_is_not_supervised(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service, live, spawned = self._setup(Path(raw))
            live.process = None
            service._supervise(live)
            self.assertEqual(spawned, [])

    def test_a_renewed_token_replaces_the_running_recorder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "token"
            service, live, spawned = self._setup(Path(raw), path)
            old = live.process
            live.token = ""  # connected anonymously
            path.write_text(_jwt(3600))
            service._supervise(live)
            self.assertTrue(old.terminated)  # type: ignore[union-attr]
            self.assertEqual(len(spawned), 1)
            self.assertEqual(live.token, path.read_text())
            # the same token again changes nothing
            live.last_spawn = time.time() - 60
            service._supervise(live)
            self.assertEqual(len(spawned), 1)


if __name__ == "__main__":
    unittest.main()
