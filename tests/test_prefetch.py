import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.domain.models import ScheduleEvent, ScheduleResponse, ScheduleSession
from app.services.prefetch import SessionPrefetchScheduler


class _InMemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    def read_json(self, key: str):
        return self._store.get(key)

    def write_json(self, key: str, payload) -> None:
        self._store[key] = payload


class _CountingHistorical:
    """Answers get_schedule with one event/session and fails every fetch - the shape of a
    session that has not run yet, which is exactly the case that must not be retried on every
    tick."""

    def __init__(self, event_date: str) -> None:
        self.event_date = event_date
        self.load_session_calls = 0
        self.race_playback_calls = 0

    async def get_schedule(self, year: int, refresh: bool = False) -> ScheduleResponse:
        return ScheduleResponse(
            season_year=year,
            events=[
                ScheduleEvent(
                    season_year=year,
                    round_number=15,
                    event_name="Azerbaijan Grand Prix",
                    event_date=self.event_date,
                    session_names=["Race"],
                )
            ],
            cache_hit=False,
            cache_key="test",
        )

    async def load_session(self, request):
        self.load_session_calls += 1
        raise RuntimeError("fastf1.exceptions.RateLimitExceededError: any API: 500 calls/h")

    async def race_playback(self, request):
        self.race_playback_calls += 1
        raise RuntimeError("fastf1.exceptions.RateLimitExceededError: any API: 500 calls/h")


def _run(coro):
    return asyncio.run(coro)


class PrefetchCooldownTests(unittest.TestCase):
    """The incident: round 15's Race, a day ahead of the event date, was reattempted every five
    minutes (the default tick interval) with no backoff at all, because a session too early to
    have data is exempt from the give-up budget - and "exempt from the budget" had come to mean
    "retried without limit", which alone was enough to exhaust FastF1's 500-calls/hour limiter."""

    def _scheduler(self, historical: _CountingHistorical) -> SessionPrefetchScheduler:
        return SessionPrefetchScheduler(
            historical=historical,
            cache=_InMemoryCache(),
            interval_seconds=300,
            lookback_hours=96,
            live=None,
        )

    def test_a_session_too_early_is_not_reattempted_within_the_cooldown(self) -> None:
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        historical = _CountingHistorical(event_date=tomorrow)
        scheduler = self._scheduler(historical)

        _run(scheduler._tick())
        _run(scheduler._tick())

        self.assertEqual(historical.load_session_calls, 1, "a second tick inside the cooldown re-fetched the session")

    def test_a_session_is_retried_again_once_the_cooldown_has_passed(self) -> None:
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        historical = _CountingHistorical(event_date=tomorrow)
        cache = _InMemoryCache()
        scheduler = SessionPrefetchScheduler(
            historical=historical, cache=cache, interval_seconds=300, lookback_hours=96, live=None
        )

        _run(scheduler._tick())
        self.assertEqual(historical.load_session_calls, 1)

        # Back-date the recorded attempt past the cooldown, standing in for real time passing.
        state = cache.read_json("runtime/prefetch-state.json")
        key = next(iter(state["sessions"]))
        stale = datetime.now(UTC) - timedelta(hours=2)
        state["sessions"][key]["last_attempt_at"] = stale.isoformat()
        cache.write_json("runtime/prefetch-state.json", state)

        _run(scheduler._tick())
        self.assertEqual(historical.load_session_calls, 2, "a tick after the cooldown expired did not retry")

    def test_a_genuine_miss_past_the_event_date_still_does_not_burst_within_the_cooldown(self) -> None:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        historical = _CountingHistorical(event_date=yesterday)
        scheduler = self._scheduler(historical)

        _run(scheduler._tick())
        _run(scheduler._tick())

        self.assertEqual(historical.load_session_calls, 1, "a real miss was still reattempted inside the cooldown")


class _RecordingHistorical:
    """One event with one race that answers with laps, and a record of every load asked of it."""

    def __init__(self, starts_at: str | None, scheduled_end: str | None = None) -> None:
        self.starts_at = starts_at
        self.scheduled_end = scheduled_end
        self.sessions: list = []
        self.playbacks: list = []

    async def get_schedule(self, year: int, refresh: bool = False) -> ScheduleResponse:
        today = datetime.now(UTC).date().isoformat()
        return ScheduleResponse(
            season_year=year,
            events=[
                ScheduleEvent(
                    season_year=year,
                    round_number=19,
                    event_name="Mexico City Grand Prix",
                    event_date=today,
                    session_names=["Race"],
                    sessions=[ScheduleSession(session_name="Race", starts_at=self.starts_at)],
                )
            ],
            cache_hit=False,
            cache_key="test",
        )

    async def load_session(self, request):
        self.sessions.append(request)
        return SimpleNamespace(
            laps=[SimpleNamespace(driver_code="VER", lap_number=1)],
            descriptor=SimpleNamespace(scheduled_end=self.scheduled_end),
        )

    async def race_playback(self, request):
        self.playbacks.append(request)
        raise RuntimeError("no playback in this test")


class PrefetchAfterTheSessionTests(unittest.TestCase):
    """The incident: Mexico 2026's Race was prefetched at 08:03 on race day, twelve hours before it
    was run. The archive held two laps of it then; they were kept in FastF1's cache as the race,
    the race was marked done, and every load after it - a reload included - served those two laps."""

    def _tick(self, historical: _RecordingHistorical, cache: _InMemoryCache | None = None) -> dict:
        cache = cache or _InMemoryCache()
        scheduler = SessionPrefetchScheduler(
            historical=historical, cache=cache, interval_seconds=300, lookback_hours=96, live=None
        )
        _run(scheduler._tick())
        return (cache.read_json("runtime/prefetch-state.json") or {}).get("sessions", {})

    def test_a_session_not_over_yet_is_not_loaded(self) -> None:
        for started in (timedelta(hours=-12), timedelta(hours=1), timedelta(hours=3)):
            starts_at = (datetime.now(UTC) - started).isoformat()
            historical = _RecordingHistorical(starts_at=starts_at)

            state = self._tick(historical)

            self.assertEqual(historical.sessions, [], f"a race that started {started} ago was loaded")
            self.assertEqual(state, {})

    def test_a_session_over_is_read_from_the_archive_again_and_kept(self) -> None:
        starts_at = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
        historical = _RecordingHistorical(starts_at=starts_at)

        state = self._tick(historical)

        self.assertEqual(len(historical.sessions), 1)
        self.assertTrue(historical.sessions[0].refresh, "the session was read from what an earlier load left")
        self.assertTrue(
            all(request.refresh for request in historical.playbacks),
            "the playback was read from what an earlier load left",
        )
        self.assertTrue(state[f"{datetime.now(UTC).year}:19:Race"]["done"])

    def test_a_session_whose_end_is_not_known_is_not_taken_as_final(self) -> None:
        historical = _RecordingHistorical(starts_at=None, scheduled_end=None)

        state = self._tick(historical)

        self.assertEqual(len(historical.sessions), 1)
        self.assertFalse(state[f"{datetime.now(UTC).year}:19:Race"]["done"])


if __name__ == "__main__":
    unittest.main()
