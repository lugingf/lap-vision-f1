import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import load_settings


class _DummyCache:
    """A cache that holds nothing, for tests about what is computed rather than what is stored."""

    def read_json(self, _key: str):
        return None

    def write_json(self, _key: str, _payload) -> None:
        pass


class _DummyExecutor:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def shutdown(self, wait: bool = False, cancel_futures: bool = True) -> None:
        pass


with patch("concurrent.futures.ProcessPoolExecutor", _DummyExecutor):
    from app.main import app


class ConfigSmokeTests(unittest.TestCase):
    def test_load_settings_has_token_and_dirs(self) -> None:
        settings = load_settings()

        self.assertTrue(settings.internal_token)
        self.assertGreater(settings.port, 0)
        self.assertGreaterEqual(settings.worker_processes, 1)
        self.assertTrue(settings.fastf1_cache_dir)
        self.assertTrue(settings.data_cache_dir)


class FastAPISmokeTests(unittest.TestCase):
    def test_app_metadata_and_core_routes_exist(self) -> None:
        route_paths = {getattr(route, "path", None) for route in app.routes}

        self.assertEqual(app.title, "Lap Vision F1")
        self.assertIn("/healthz", route_paths)
        self.assertIn("/v1/overview", route_paths)
        self.assertIn("/v1/sessions/load", route_paths)


class RacePlaybackWindowTests(unittest.TestCase):
    """The window used to be part of the cache key.

    Every position of the scrub slider asked for a different window, so every request was a miss,
    and a miss means loading the whole session from FastF1 again — which takes longer than the
    caller waits. The detailed mode therefore never worked outside a cold start. One entry per race
    and resolution, windows cut out of it.
    """

    def _request(self, **overrides):
        from app.domain.models import RacePlaybackRequest

        payload = {"year": 2026, "event": 5, "session": "Race", "sample_step_ms": 1000}
        payload.update(overrides)
        return RacePlaybackRequest(**payload)

    def test_cache_key_does_not_depend_on_the_window(self) -> None:
        from app.services.historical import _cache_key_for_race_playback

        whole = _cache_key_for_race_playback(self._request())
        windowed = _cache_key_for_race_playback(
            self._request(window_start_ms=1_800_000, window_end_ms=1_810_000)
        )

        self.assertEqual(whole, windowed)

    def test_cache_key_still_separates_resolutions(self) -> None:
        from app.services.historical import _cache_key_for_race_playback

        coarse = _cache_key_for_race_playback(self._request(sample_step_ms=1000))
        fine = _cache_key_for_race_playback(self._request(sample_step_ms=100))

        self.assertNotEqual(coarse, fine)

    def _payload(self):
        return {
            "available": True,
            "window_start_ms": 0,
            "window_end_ms": 10_000,
            "drivers": [
                {
                    "driver_code": "VER",
                    "samples": [{"time_ms": step * 1000, "x": step, "y": 0} for step in range(11)],
                }
            ],
        }

    def test_a_window_keeps_only_what_was_asked_for(self) -> None:
        from app.services.historical import _slice_playback_window

        sliced = _slice_playback_window(self._payload(), 4_000, 6_000, 1000)
        times = [sample["time_ms"] for sample in sliced["drivers"][0]["samples"]]

        # One step either side is kept on purpose: the client interpolates between samples and
        # needs something to interpolate towards at the edges.
        self.assertEqual(times, [3_000, 4_000, 5_000, 6_000, 7_000])
        self.assertEqual(sliced["window_start_ms"], 3_000)
        self.assertEqual(sliced["window_end_ms"], 7_000)

    def test_slicing_leaves_the_cached_payload_alone(self) -> None:
        from app.services.historical import _slice_playback_window

        payload = self._payload()
        _slice_playback_window(payload, 4_000, 6_000, 1000)

        # The cached object is shared between requests; slicing it in place would shrink the race
        # for everyone who asked after.
        self.assertEqual(len(payload["drivers"][0]["samples"]), 11)
        self.assertEqual(payload["window_end_ms"], 10_000)

    def test_no_window_returns_the_whole_race(self) -> None:
        from app.services.historical import _slice_playback_window

        payload = self._payload()
        self.assertIs(_slice_playback_window(payload, None, None, 1000), payload)

    def test_an_unavailable_playback_is_passed_through(self) -> None:
        from app.services.historical import _slice_playback_window

        payload = {"available": False, "message": "no position data"}
        self.assertIs(_slice_playback_window(payload, 0, 1000, 1000), payload)


class InFlightComputationTests(unittest.IsolatedAsyncioTestCase):
    """One computation per cache key, however many callers ask for it.

    A session FastF1 has not loaded takes minutes, and the reader rarely waits: the request dies
    at a proxy or a browser, but the process-pool job does not, because a running one cannot be
    cancelled. Every further attempt used to submit a second job for the work already under way,
    and with two worker processes a few impatient readers filled the pool with duplicates of one
    computation — which is what made the site slow exactly when it was already slow.
    """

    def _service(self):
        from app.services.historical import HistoricalService

        with patch("app.services.historical.ProcessPoolExecutor", _DummyExecutor):
            service = HistoricalService(Path("/tmp/fastf1"), _DummyCache(), worker_processes=2)

        return service

    async def test_concurrent_callers_share_one_computation(self) -> None:
        service = self._service()
        started = 0
        release = asyncio.Event()

        async def slow() -> dict:
            nonlocal started
            started += 1
            await release.wait()
            return {"ok": True}

        def submit(*_args):
            return asyncio.ensure_future(slow())

        with patch.object(service, "_submit", side_effect=submit):
            waiters = [asyncio.ensure_future(service._compute_once("key", object())) for _ in range(4)]
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*waiters)

        self.assertEqual(started, 1, "the same session was computed more than once")
        self.assertEqual(results, [{"ok": True}] * 4)

    async def test_a_caller_giving_up_does_not_cancel_the_work(self) -> None:
        service = self._service()
        finished = asyncio.Event()

        async def slow() -> dict:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            finished.set()
            return {"ok": True}

        def submit(*_args):
            return asyncio.ensure_future(slow())

        with patch.object(service, "_submit", side_effect=submit):
            giving_up = asyncio.ensure_future(service._compute_once("key", object()))
            await asyncio.sleep(0)
            giving_up.cancel()

            await asyncio.wait_for(finished.wait(), timeout=1)

    async def test_a_later_caller_starts_a_fresh_computation(self) -> None:
        service = self._service()
        started = 0

        async def quick() -> dict:
            nonlocal started
            started += 1
            return {"ok": True}

        def submit(*_args):
            return asyncio.ensure_future(quick())

        with patch.object(service, "_submit", side_effect=submit):
            await service._compute_once("key", object())
            await service._compute_once("key", object())

        self.assertEqual(started, 2, "a finished computation was reused instead of being redone")
