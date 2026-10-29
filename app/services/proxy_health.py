"""Whether each registered proxy actually gets through to F1.

f1-proxy-relay registers a proxy as soon as its port answers, but an open port says nothing about
what is behind it: the tunnel can be half-dead, the home connection down, or F1 can have started
refusing that address. So every proxy is asked, every couple of minutes, for the small file the
live timing publishes - refused with 403 from the server's own address, served through a working
proxy - and the answers are kept for the status page. Only changes are logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

_logger = logging.getLogger("lap-vision-f1.proxy-health")

PROBE_URL = "https://livetiming.formula1.com/static/StreamingStatus.json"
PROBE_TIMEOUT_SECONDS = 20
CHECK_INTERVAL_SECONDS = 120

# The probe's HTTP status through one proxy, or None and why there was no answer at all.
ProbeResult = tuple[int | None, str | None]


def _to_iso(epoch_seconds: float | None) -> str | None:
    return None if epoch_seconds is None else datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def probe(proxy_url: str) -> ProbeResult:
    try:
        response = requests.get(
            PROBE_URL,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return response.status_code, None


@dataclass
class _ProxyState:
    ok: bool = False
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    checked_at: float | None = None
    last_ok_at: float | None = None


class ProxyHealth:
    def __init__(
        self,
        proxy_urls: Callable[[], list[str]],
        probe: Callable[[str], ProbeResult] = probe,
        interval_seconds: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._proxy_urls = proxy_urls
        self._probe = probe
        self._interval_seconds = interval_seconds
        self._states: dict[str, _ProxyState] = {}
        self.checked_at: float | None = None

    def _timed_probe(self, proxy_url: str) -> tuple[int | None, str | None, int]:
        started = time.monotonic()
        status_code, error = self._probe(proxy_url)
        return status_code, error, round((time.monotonic() - started) * 1000)

    async def check(self) -> None:
        """Probe every registered proxy at once. One the relay has taken off the list is forgotten."""
        urls = self._proxy_urls()
        results = await asyncio.gather(*(asyncio.to_thread(self._timed_probe, url) for url in urls))
        now = time.time()
        states: dict[str, _ProxyState] = {}
        for url, (status_code, error, latency_ms) in zip(urls, results, strict=True):
            state = self._states.get(url) or _ProxyState()
            ok = status_code == 200
            if ok != state.ok or state.checked_at is None:
                if ok:
                    _logger.info("proxy-health: %s reaches F1 (%d ms)", url, latency_ms)
                else:
                    _logger.warning("proxy-health: %s does not reach F1: %s", url, error or f"HTTP {status_code}")
            state.ok = ok
            state.status_code = status_code
            state.latency_ms = latency_ms
            state.error = error
            state.checked_at = now
            if ok:
                state.last_ok_at = now
            states[url] = state
        self._states = states
        self.checked_at = now

    def status(self) -> dict[str, Any]:
        """What the status page is told: whether F1 is reachable at all, and through which proxy."""
        proxies = [
            {
                "url": url,
                "ok": state.ok,
                "status_code": state.status_code,
                "latency_ms": state.latency_ms,
                "error": state.error,
                "checked_at": _to_iso(state.checked_at),
                "last_ok_at": _to_iso(state.last_ok_at),
            }
            for url, state in self._states.items()
        ]
        return {
            "ok": any(proxy["ok"] for proxy in proxies),
            "checked_at": _to_iso(self.checked_at),
            "probe_url": PROBE_URL,
            "proxies": proxies,
        }

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - a failed round must not end the checking
                _logger.exception("proxy-health: check failed")
            await asyncio.sleep(self._interval_seconds)
