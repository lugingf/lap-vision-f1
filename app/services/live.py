from __future__ import annotations

import ast
import asyncio
import base64
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from multiprocessing import Process
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.domain.models import LiveSessionRequest, LiveSessionStatus
from app.services.historical import HistoricalService, _apply_proxy_env, fetch_live_snapshot_payload
from app.services.live_state import LiveState


def read_token(token_file: Path | None) -> str:
    """The F1TV subscription token, or an empty string when there is none.

    The feed answers without it, with the timing and without the cars: positions and car data are
    sent only to a subscriber. FastF1's own way of getting one opens a login in a browser and waits
    for it, which on a server is a recorder that hangs and records nothing; here the token is a file
    somebody put there, and its absence is a way of connecting, not a wait.
    """
    if token_file is None:
        return ""
    try:
        return token_file.read_text().strip()
    except OSError:
        return ""


def token_status(token_file: Path | None) -> dict[str, Any]:
    """What the token on disk is, read without verifying it: whether there is one and until when."""
    token = read_token(token_file)
    if not token:
        return {"mode": "anonymous", "expires_at": None, "expired": False}
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    except (IndexError, ValueError):
        return {"mode": "f1tv", "expires_at": None, "expired": False}
    expires = claims.get("exp")
    expired = bool(expires) and expires < time.time()
    return {
        "mode": "anonymous" if expired else "f1tv",
        "expires_at": _to_iso(expires) if expires else None,
        "expired": expired,
    }


def _run_recorder_process(raw_file: str, proxy_url: str | None, token_file: str | None = None) -> None:
    """Entry point for the dedicated recorder process (kept top-level: multiprocessing on
    macOS/Windows uses 'spawn', which needs a picklable, importable target).

    Runs a single, blocking connection to F1's live SignalR feed and appends raw messages to
    raw_file. One proxy is bound for the whole connection: unlike the historical fetch_*
    functions, a dropped socket here means a dropped live feed, not a retriable single request,
    so there is no per-attempt proxy pool here yet.
    """
    import fastf1.livetiming.client as livetiming_client  # type: ignore

    SignalRClient = livetiming_client.SignalRClient  # noqa: N806

    # Never FastF1's interactive login: see read_token.
    path = Path(token_file) if token_file else None
    livetiming_client.get_auth_token = lambda: read_token(path)

    if proxy_url:
        _apply_proxy_env(proxy_url)
        parsed = urlparse(proxy_url)
        if parsed.scheme.startswith("socks") and parsed.hostname and parsed.port:
            import socket

            import socks  # type: ignore

            socks.set_default_proxy(socks.SOCKS5, parsed.hostname, parsed.port, rdns=True)
            socket.socket = socks.socksocket  # process-local: this is a dedicated child process

    client = SignalRClient(filename=raw_file, filemode="a", timeout=0)
    client.start()


def _slugify(value: str | int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")


def _to_iso(epoch_seconds: float | None) -> str | None:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


@dataclass
class _LiveSession:
    request: LiveSessionRequest
    raw_file: Path
    process: Process | None
    refresh_task: asyncio.Task[None] | None = None
    tail_task: asyncio.Task[None] | None = None
    replay_task: asyncio.Task[None] | None = None
    state: LiveState = field(default_factory=LiveState)
    snapshot: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    last_update: float | None = None
    last_error: str | None = None

    @property
    def alive(self) -> bool:
        if self.process is not None:
            return self.process.is_alive()
        return self.replay_task is not None and not self.replay_task.done()


class LiveSessionService:
    """Keeps at most one active live recording per (year, event, session) key.

    Architecture: a dedicated child process holds the live SignalR connection open and appends
    raw messages to a file (this is what FastF1's own docs call "recording"). A background task
    in the main event loop re-parses that growing file into a normalized session-bundle snapshot
    every `quantum_seconds` ("chunked" / "quantized" refresh, since FastF1 has no incremental
    per-message API - `fastf1.livetiming.data.LiveTimingData` always reads the whole file). Callers
    poll `snapshot()` for the latest normalized data instead of triggering a re-parse themselves.
    """

    def __init__(
        self,
        historical: HistoricalService,
        live_data_dir: Path,
        quantum_seconds: int,
        token_file: Path | None = None,
        snapshot_enabled: bool = True,
    ) -> None:
        self._historical = historical
        self._live_data_dir = live_data_dir
        self._quantum_seconds = quantum_seconds
        self._token_file = token_file
        # The session bundle FastF1 builds from the whole recording, every few seconds. The live screen
        # reads the state kept message by message and does not need it; it stays for the session page's
        # recorder panel, and can be switched off to spare the processor a long race's recording.
        self._snapshot_enabled = snapshot_enabled
        self._sessions: dict[str, _LiveSession] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(request: LiveSessionRequest) -> str:
        return f"{request.year}:{_slugify(request.event)}:{request.session.strip().lower()}"

    async def start(self, request: LiveSessionRequest) -> LiveSessionStatus:
        async with self._lock:
            key = self._key(request)
            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                return self._status(existing)
            if existing is not None:
                self._cleanup(existing)

            self._live_data_dir.mkdir(parents=True, exist_ok=True)
            raw_file = self._live_data_dir / f"{key.replace(':', '_')}.txt"

            proxy_urls = self._historical.get_proxy_urls()
            proxy_url = random.choice(proxy_urls) if proxy_urls else None

            token_file = str(self._token_file) if self._token_file else None
            process = Process(target=_run_recorder_process, args=(str(raw_file), proxy_url, token_file), daemon=True)
            process.start()

            live_session = _LiveSession(request=request, raw_file=raw_file, process=process)
            self._sessions[key] = live_session
            live_session.tail_task = asyncio.create_task(self._tail_loop(key))
            if self._snapshot_enabled:
                live_session.refresh_task = asyncio.create_task(self._refresh_loop(key))
            return self._status(live_session)

    async def replay(self, request: LiveSessionRequest, source: Path, speed: float = 1.0) -> LiveSessionStatus:
        """Play a recording back as if it were arriving now. For trying the live screen outside a
        session: nothing connects anywhere."""
        async with self._lock:
            key = self._key(request)
            existing = self._sessions.get(key)
            if existing is not None:
                self._cleanup(existing)
            live_session = _LiveSession(request=request, raw_file=source, process=None)
            self._sessions[key] = live_session
            live_session.replay_task = asyncio.create_task(self._replay_loop(key, source, max(speed, 0.1)))
            return self._status(live_session)

    async def stop(self, request: LiveSessionRequest) -> LiveSessionStatus:
        async with self._lock:
            key = self._key(request)
            live_session = self._sessions.pop(key, None)
            if live_session is None:
                return LiveSessionStatus(running=False)
            self._cleanup(live_session)
            return LiveSessionStatus(
                running=False,
                year=request.year,
                event=request.event,
                session=request.session,
            )

    def is_running(self, year: int, event: str | int, session: str) -> bool:
        live_session = self._sessions.get(self._key(LiveSessionRequest(year=year, event=event, session=session)))
        return live_session is not None and live_session.alive

    def status(self, request: LiveSessionRequest) -> LiveSessionStatus:
        live_session = self._sessions.get(self._key(request))
        if live_session is None:
            return LiveSessionStatus(running=False)
        return self._status(live_session)

    def snapshot(self, request: LiveSessionRequest) -> dict[str, Any] | None:
        live_session = self._sessions.get(self._key(request))
        if live_session is None:
            return None
        return live_session.snapshot

    def _live(self, request: LiveSessionRequest) -> _LiveSession | None:
        return self._sessions.get(self._key(request))

    def state(self, request: LiveSessionRequest) -> dict[str, Any] | None:
        """What the screen draws, or None when nothing is being recorded for the session."""
        live_session = self._live(request)
        if live_session is None:
            return None
        payload = live_session.state.snapshot()
        payload["running"] = live_session.alive
        payload["auth"] = token_status(self._token_file)
        return payload

    def telemetry(
        self, request: LiveSessionRequest, driver: str, seconds: int, lap: bool = False
    ) -> dict[str, Any] | None:
        live_session = self._live(request)
        return None if live_session is None else live_session.state.telemetry(driver, seconds, lap)

    def outline(self, request: LiveSessionRequest) -> dict[str, Any] | None:
        live_session = self._live(request)
        return None if live_session is None else live_session.state.outline_payload()

    def _outline_file(self, circuit: str) -> Path:
        return self._live_data_dir / "outlines" / f"{_slugify(circuit)}.json"

    def _sync_outline(self, live_session: _LiveSession) -> None:
        """A circuit is drawn once: the outline a session has closed is kept, and a later session at
        the same circuit starts with it. A replay is not a circuit: it neither keeps nor reads one."""
        if live_session.process is None:
            return
        state = live_session.state
        circuit = state.circuit_key()
        if circuit is None:
            return
        path = self._outline_file(circuit)
        if state.outline.closed:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(state.outline_payload()["polyline"]))
            return
        if not state.outline.points and path.exists():
            try:
                state.outline.load(json.loads(path.read_text()))
            except (OSError, ValueError, KeyError, TypeError):
                return

    def trails(self, request: LiveSessionRequest, seconds: int) -> dict[str, Any] | None:
        live_session = self._live(request)
        return None if live_session is None else {"trails": live_session.state.trails(seconds)}

    def history(self, request: LiveSessionRequest) -> dict[str, Any] | None:
        live_session = self._live(request)
        return None if live_session is None else live_session.state.history()

    def _cleanup(self, live_session: _LiveSession) -> None:
        for task in (live_session.refresh_task, live_session.tail_task, live_session.replay_task):
            if task is not None:
                task.cancel()
        if live_session.process is not None and live_session.process.is_alive():
            live_session.process.terminate()
            live_session.process.join(timeout=5)

    def _status(self, live_session: _LiveSession) -> LiveSessionStatus:
        return LiveSessionStatus(
            running=live_session.alive,
            year=live_session.request.year,
            event=live_session.request.event,
            session=live_session.request.session,
            started_at=_to_iso(live_session.started_at),
            last_update=_to_iso(live_session.last_update),
            last_error=live_session.last_error,
            raw_bytes=live_session.raw_file.stat().st_size if live_session.raw_file.exists() else 0,
            snapshot_available=live_session.snapshot is not None,
            messages=live_session.state.messages,
            auth=token_status(self._token_file),
            has_car_data=bool(live_session.state.car),
            has_positions=bool(live_session.state.position),
        )

    async def _tail_loop(self, key: str) -> None:
        """Follow the recording as the recorder writes it, handing every complete line to the state.

        A message is one line, a few kilobytes at the most. The recorder may be half way through the
        last one when it is read, so what follows the final newline is kept for the next time."""
        offset = 0
        pending = b""
        while True:
            live_session = self._sessions.get(key)
            if live_session is None:
                return
            try:
                if live_session.raw_file.exists():
                    with live_session.raw_file.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                    if chunk:
                        offset += len(chunk)
                        pending += chunk
                        *lines, pending = pending.split(b"\n")
                        for line in lines:
                            self._feed_line(live_session, line)
                        live_session.last_update = time.time()
                        self._sync_outline(live_session)
            except OSError as exc:
                live_session.last_error = str(exc)
            await asyncio.sleep(0.5)

    @staticmethod
    def _feed_line(live_session: _LiveSession, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            topic, payload, stamp = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return
        live_session.state.feed(topic, payload, stamp)

    async def _replay_loop(self, key: str, source: Path, speed: float) -> None:
        """Feed a recording to the state at the pace it was recorded, divided by speed."""
        live_session = self._sessions.get(key)
        if live_session is None:
            return
        previous: float | None = None
        for line in source.read_text().splitlines():
            try:
                topic, payload, stamp = ast.literal_eval(line)
            except (ValueError, SyntaxError):
                continue
            moment = None
            if stamp:
                try:
                    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    moment = None
            if moment is not None and previous is not None and moment > previous:
                await asyncio.sleep(min((moment - previous) / speed, 2.0))
            if moment is not None:
                previous = moment
            live_session.state.feed(topic, payload, stamp)
            live_session.last_update = time.time()
            self._sync_outline(live_session)

    async def _refresh_loop(self, key: str) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._quantum_seconds)
            live_session = self._sessions.get(key)
            if live_session is None:
                return
            if not live_session.raw_file.exists() or live_session.raw_file.stat().st_size == 0:
                continue
            try:
                payload = await loop.run_in_executor(
                    self._historical.executor,
                    fetch_live_snapshot_payload,
                    str(live_session.raw_file),
                    str(self._historical.fastf1_cache_dir),
                    live_session.request.model_dump(),
                )
            except Exception as exc:  # noqa: BLE001 - keep refreshing across transient parse errors
                live_session.last_error = str(exc)
                continue
            live_session.snapshot = payload
            live_session.last_update = time.time()
            live_session.last_error = None
