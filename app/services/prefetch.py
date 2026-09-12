from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.cache import CacheManager
from app.domain.models import RacePlaybackRequest, SessionRequest
from app.services.historical import HistoricalService
from app.services.live import LiveSessionService

_logger = logging.getLogger("lap-vision-f1.prefetch")

_STATE_KEY = "runtime/prefetch-state.json"
_MAX_ATTEMPTS = 12
_POST_SESSION_BUFFER = timedelta(minutes=45)


class SessionPrefetchScheduler:
    """Proactively warms the cache for the current race weekend's sessions.

    A session's data only exists once F1 has actually published it, and the schedule endpoint
    doesn't carry per-session start/end times - only the weekend's overall event_date - so this
    can't know exactly when a session finishes. Instead it just periodically retries every
    session of any event within `lookback_hours` of now, and remembers (on the same shared cache
    volume used everywhere else) which ones already returned real data so it stops re-fetching
    them. A session that never gets real data (cancelled, or the schedule is wrong) is retried at
    most `_MAX_ATTEMPTS` times before being left alone.

    Runs one session at a time, never in parallel with itself, and reuses the exact same
    fetch path (and its rate-limit-respecting concurrent-prewarm) as a normal user request - so
    from F1's API's perspective this looks like a normal, occasional user, not a scraper.

    Skips any session that currently has an active live recording (see LiveSessionService):
    both share HistoricalService's process pool, and a slow historical race_playback fetch could
    starve live mode's every-few-seconds snapshot refresh of a worker for the duration of that
    fetch - exactly when low latency matters most. Live mode is already the right data source for
    an in-progress session, so there is nothing useful for prefetch to add there anyway.
    """

    def __init__(
        self,
        historical: HistoricalService,
        cache: CacheManager,
        interval_seconds: int,
        lookback_hours: int,
        live: LiveSessionService | None = None,
    ) -> None:
        self._historical = historical
        self._cache = cache
        self._interval_seconds = interval_seconds
        self._lookback_hours = lookback_hours
        self._live = live
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _load_state(self) -> dict[str, dict[str, Any]]:
        stored = self._cache.read_json(_STATE_KEY)
        if not stored:
            return {}
        return stored.get("sessions") or {}

    def _save_state(self, state: dict[str, dict[str, Any]]) -> None:
        self._cache.write_json(_STATE_KEY, {"sessions": state})

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                _logger.exception("prefetch tick failed")
            await asyncio.sleep(self._interval_seconds)

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        schedule = await self._historical.get_schedule(now.year)
        state = self._load_state()

        for event in schedule.events:
            event_date = self._parse_event_date(event.event_date)
            if not self._event_in_window(event_date, now, self._lookback_hours):
                continue
            for session_name in event.session_names:
                key = f"{now.year}:{event.round_number}:{session_name}"
                entry = state.get(key, {"done": False, "attempts": 0})
                if entry.get("done") or entry.get("attempts", 0) >= _MAX_ATTEMPTS:
                    continue
                if self._live is not None and self._live.is_running(now.year, event.round_number, session_name):
                    _logger.info("prefetch: skipping %s, live recording is active", key)
                    continue

                done, had_data = await self._prefetch_one(now.year, event.round_number, session_name)
                if had_data:
                    # The session exists and is returning something - either still in progress
                    # (not `done` yet) or just finished. Either way this isn't a "session that
                    # will never have data", so don't spend the give-up budget on it.
                    entry["attempts"] = 0
                elif event_date is not None and now.date() >= event_date:
                    # No data yet, but the session's day has arrived - this is a real miss.
                    entry["attempts"] = entry.get("attempts", 0) + 1
                # else: too early for this session to have data yet - free retry, no penalty.
                entry["done"] = done
                state[key] = entry
                self._save_state(state)

                # One session at a time, with a breather in between - this is a background
                # convenience job, not a race to warm everything as fast as possible.
                await asyncio.sleep(2)

    @staticmethod
    def _parse_event_date(event_date: str | None) -> date | None:
        if not event_date:
            return None
        try:
            return date.fromisoformat(event_date)
        except ValueError:
            return None

    @staticmethod
    def _event_in_window(event_date: date | None, now: datetime, lookback_hours: int) -> bool:
        if event_date is None:
            return False
        today = now.date()
        lookback_days = max(1, -(-lookback_hours // 24))  # ceil division
        # Practice/qualifying happen in the days before event_date (usually the race day), so
        # look back the configured window as well as a day ahead in case of timezone edge cases.
        return today - timedelta(days=lookback_days) <= event_date <= today + timedelta(days=1)

    async def _prefetch_one(self, year: int, round_number: int, session_name: str) -> tuple[bool, bool]:
        """Returns (done, had_data). `done` only turns True once the session's own scheduled end
        time (plus a buffer) has passed - not just because *some* data came back, since a session
        in progress will happily return partial laps/positions well before it's actually over.
        """
        _logger.info("prefetch: %s round %s %s", year, round_number, session_name)
        bundle = None
        got_laps = False
        try:
            bundle = await self._historical.load_session(
                SessionRequest(
                    year=year,
                    event=round_number,
                    session=session_name,
                    include_laps=True,
                    include_weather=True,
                    include_messages=True,
                    include_telemetry=False,
                )
            )
            got_laps = len(bundle.laps) > 0
        except Exception:
            _logger.exception("prefetch: session load failed for %s round %s %s", year, round_number, session_name)

        got_playback = False
        try:
            playback = await self._historical.race_playback(
                RacePlaybackRequest(
                    year=year,
                    event=round_number,
                    session=session_name,
                    include_telemetry=False,
                )
            )
            got_playback = playback.available
        except Exception:
            _logger.exception("prefetch: race playback failed for %s round %s %s", year, round_number, session_name)

        had_data = got_laps or got_playback
        if not had_data:
            return False, False

        scheduled_end = bundle.descriptor.scheduled_end if bundle is not None else None
        if not scheduled_end:
            # No scheduled end time known - fall back to trusting the data as final, as before.
            return True, True

        try:
            end_at = datetime.fromisoformat(scheduled_end)
        except ValueError:
            return True, True
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=UTC)

        is_over = datetime.now(UTC) >= end_at + _POST_SESSION_BUFFER
        return is_over, True
