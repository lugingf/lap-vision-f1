from __future__ import annotations

import asyncio
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


def _run_recorder_process(raw_file: str, proxy_url: str | None) -> None:
    """Entry point for the dedicated recorder process (kept top-level: multiprocessing on
    macOS/Windows uses 'spawn', which needs a picklable, importable target).

    Runs a single, blocking connection to F1's live SignalR feed and appends raw messages to
    raw_file. One proxy is bound for the whole connection: unlike the historical fetch_*
    functions, a dropped socket here means a dropped live feed, not a retriable single request,
    so there is no per-attempt proxy pool here yet.
    """
    from fastf1.livetiming.client import SignalRClient  # type: ignore

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
    process: Process
    refresh_task: asyncio.Task[None] | None = None
    snapshot: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    last_update: float | None = None
    last_error: str | None = None


class LiveSessionService:
    """Keeps at most one active live recording per (year, event, session) key.

    Architecture: a dedicated child process holds the live SignalR connection open and appends
    raw messages to a file (this is what FastF1's own docs call "recording"). A background task
    in the main event loop re-parses that growing file into a normalized session-bundle snapshot
    every `quantum_seconds` ("chunked" / "quantized" refresh, since FastF1 has no incremental
    per-message API - `fastf1.livetiming.data.LiveTimingData` always reads the whole file). Callers
    poll `snapshot()` for the latest normalized data instead of triggering a re-parse themselves.
    """

    def __init__(self, historical: HistoricalService, live_data_dir: Path, quantum_seconds: int) -> None:
        self._historical = historical
        self._live_data_dir = live_data_dir
        self._quantum_seconds = quantum_seconds
        self._sessions: dict[str, _LiveSession] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(request: LiveSessionRequest) -> str:
        return f"{request.year}:{_slugify(request.event)}:{request.session.strip().lower()}"

    async def start(self, request: LiveSessionRequest) -> LiveSessionStatus:
        async with self._lock:
            key = self._key(request)
            existing = self._sessions.get(key)
            if existing is not None and existing.process.is_alive():
                return self._status(existing)
            if existing is not None:
                self._cleanup(existing)

            self._live_data_dir.mkdir(parents=True, exist_ok=True)
            raw_file = self._live_data_dir / f"{key.replace(':', '_')}.txt"

            proxy_urls = self._historical.get_proxy_urls()
            proxy_url = random.choice(proxy_urls) if proxy_urls else None

            process = Process(target=_run_recorder_process, args=(str(raw_file), proxy_url), daemon=True)
            process.start()

            live_session = _LiveSession(request=request, raw_file=raw_file, process=process)
            self._sessions[key] = live_session
            live_session.refresh_task = asyncio.create_task(self._refresh_loop(key))
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

    def _cleanup(self, live_session: _LiveSession) -> None:
        if live_session.refresh_task is not None:
            live_session.refresh_task.cancel()
        if live_session.process.is_alive():
            live_session.process.terminate()
            live_session.process.join(timeout=5)

    def _status(self, live_session: _LiveSession) -> LiveSessionStatus:
        return LiveSessionStatus(
            running=live_session.process.is_alive(),
            year=live_session.request.year,
            event=live_session.request.event,
            session=live_session.request.session,
            started_at=_to_iso(live_session.started_at),
            last_update=_to_iso(live_session.last_update),
            last_error=live_session.last_error,
            raw_bytes=live_session.raw_file.stat().st_size if live_session.raw_file.exists() else 0,
            snapshot_available=live_session.snapshot is not None,
        )

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
