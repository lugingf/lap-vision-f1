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
from app.services.f1tv import F1TVAuth, F1TVTokens
from app.services.live import LiveSessionService, _LiveSession, _run_recorder_process

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


def _store(path: Path, token: str) -> None:
    path.write_text(json.dumps({"version": 1, "value": token, "expires_at": time.time() + 3600}))


class RecorderAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastf1.internals.f1account import token_manager

        saved = (token_manager._store, token_manager._authenticate, token_manager._token)

        def restore() -> None:
            token_manager._store, token_manager._authenticate, token_manager._token = saved

        self.addCleanup(restore)

    def _run(self, tokens: F1TVTokens | None) -> tuple[dict, str]:
        import fastf1.livetiming.client as livetiming_client

        captured: dict = {}

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            def start(self) -> None:
                # what FastF1's client presents when it connects with a token
                captured["token"] = "" if captured["no_auth"] else livetiming_client.get_token().value

        with mock.patch.object(livetiming_client, "SignalRClient", FakeClient):
            _run_recorder_process("raw.txt", None, tokens)
        return captured, captured["token"]

    def test_recorder_is_anonymous_without_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for tokens in (None, F1TVTokens(Path(raw) / "missing")):
                kwargs, token = self._run(tokens)
                self.assertTrue(kwargs["no_auth"])
                self.assertEqual(token, "")

    def test_recorder_is_anonymous_with_an_expired_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "store.json"
            _store(path, _jwt(-60))
            kwargs, token = self._run(F1TVTokens(path))
        self.assertTrue(kwargs["no_auth"])
        self.assertEqual(token, "")

    def test_recorder_authenticates_with_the_kept_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "store.json"
            good = _jwt(3600)
            _store(path, good)
            kwargs, token = self._run(F1TVTokens(path))
        self.assertFalse(kwargs["no_auth"])
        self.assertEqual(token, good)

    def test_recorder_authenticates_with_a_token_put_by_hand(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            by_hand = Path(raw) / "f1auth.json"
            good = _jwt(3600)
            by_hand.write_text(good)
            kwargs, token = self._run(F1TVTokens(Path(raw) / "store.json", by_hand))
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
    def _setup(self, directory: Path, auth: F1TVAuth | None = None) -> tuple[LiveSessionService, _LiveSession, list]:
        service = LiveSessionService(SimpleNamespace(get_proxy_urls=lambda: []), directory, 5, auth, False)
        live = _LiveSession(request=REQUEST, raw_file=directory / "raw.txt", process=_FakeProcess())  # type: ignore[arg-type]
        live.last_spawn = time.time() - 60
        spawned: list[_FakeProcess] = []

        def spawn(session: _LiveSession) -> None:
            session.process = _FakeProcess()  # type: ignore[assignment]
            session.token = auth.tokens.token() if auth is not None else ""
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
            path = Path(raw) / "store.json"
            service, live, spawned = self._setup(Path(raw), F1TVAuth(F1TVTokens(path)))
            old = live.process
            live.token = ""  # connected anonymously
            fresh = _jwt(3600)
            _store(path, fresh)  # the service has signed in
            service._supervise(live)
            self.assertTrue(old.terminated)  # type: ignore[union-attr]
            self.assertEqual(len(spawned), 1)
            self.assertEqual(live.token, fresh)
            # the same token again changes nothing
            live.last_spawn = time.time() - 60
            service._supervise(live)
            self.assertEqual(len(spawned), 1)

    def test_a_renewal_leaves_a_recorder_with_a_token_alone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "store.json"
            service, live, spawned = self._setup(Path(raw), F1TVAuth(F1TVTokens(path)))
            live.token = _jwt(3600)  # connected with the old token
            _store(path, _jwt(4 * 86400))  # the service has signed in again
            service._supervise(live)
            self.assertFalse(live.process.terminated)  # type: ignore[union-attr]
            self.assertEqual(spawned, [])


class WeekendSignInTests(unittest.TestCase):
    def test_the_first_session_of_a_weekend_asks_for_a_sign_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            auth = mock.Mock(**{"status.return_value": {"mode": "f1tv"}})
            service = LiveSessionService(SimpleNamespace(get_proxy_urls=lambda: []), Path(raw), 5, auth, False)
            service._spawn = lambda session: setattr(session, "process", _FakeProcess())  # type: ignore[method-assign]

            async def start(event: str, session: str) -> None:
                await service.start(LiveSessionRequest(year=2026, event=event, session=session))
                await service.stop(LiveSessionRequest(year=2026, event=event, session=session))

            asyncio.run(start("Mexico City", "Practice 1"))
            self.assertEqual(auth.request.call_count, 1)
            asyncio.run(start("Mexico City", "Qualifying"))
            asyncio.run(start("Mexico City", "Race"))
            self.assertEqual(auth.request.call_count, 1)  # the weekend's token carries the rest
            asyncio.run(start("Sao Paulo", "Practice 1"))
            self.assertEqual(auth.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
