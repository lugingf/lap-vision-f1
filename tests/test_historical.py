import unittest

from app.services.historical import _session_bundle_has_data


class SessionBundleHasDataTests(unittest.TestCase):
    """A session bundle with nothing in it must never be cached as if it were the real answer -
    see load_session, which skips the cache write (and distrusts an existing cache entry) when
    this returns False. The incident this guards against: FastF1's @soft_exceptions decorator
    swallows a failed sub-loader (including hitting its own rate limiter) and returns an empty
    result instead of raising, so nothing upstream of this function can tell the difference
    between "genuinely nothing yet" and "silently failed" without it."""

    def test_empty_payload_has_no_data(self) -> None:
        self.assertFalse(_session_bundle_has_data({"drivers": [], "laps": [], "results": []}))

    def test_missing_keys_have_no_data(self) -> None:
        self.assertFalse(_session_bundle_has_data({}))

    def test_drivers_alone_counts_as_data(self) -> None:
        self.assertTrue(_session_bundle_has_data({"drivers": [{"driver_code": "VER"}], "laps": [], "results": []}))

    def test_laps_alone_counts_as_data(self) -> None:
        self.assertTrue(_session_bundle_has_data({"drivers": [], "laps": [{"lap_number": 1}], "results": []}))

    def test_results_alone_counts_as_data(self) -> None:
        self.assertTrue(_session_bundle_has_data({"drivers": [], "laps": [], "results": [{"position": 1}]}))


if __name__ == "__main__":
    unittest.main()
