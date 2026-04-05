import unittest
from unittest.mock import patch

from app.config import load_settings


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
