import asyncio
import base64
import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.services import f1tv
from app.services.f1tv import F1TVAuth, F1TVTokens


def _jwt(expires_in: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + expires_in}).encode()).decode().rstrip("=")
    return f"header.{claims}.signature"


def _store(path: Path, token: str) -> None:
    path.write_text(json.dumps({"version": 1, "value": token, "expires_at": time.time() + 3600}))


def _keep_fastf1_token_manager(test: unittest.TestCase) -> None:
    from fastf1.internals.f1account import token_manager

    saved = (token_manager._store, token_manager._authenticate, token_manager._token)

    def restore() -> None:
        token_manager._store, token_manager._authenticate, token_manager._token = saved

    test.addCleanup(restore)


class TokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.tokens = F1TVTokens(self.dir / "store.json", self.dir / "f1auth.json")

    def test_nothing_is_the_anonymous_feed(self) -> None:
        self.assertEqual(self.tokens.token(), "")
        self.assertEqual(self.tokens.status()["mode"], "anonymous")
        self.assertEqual(F1TVTokens(self.dir / "store.json").token(), "")

    def test_the_stored_token_comes_first(self) -> None:
        stored, by_hand = _jwt(3600), _jwt(7200)
        _store(self.tokens.store_file, stored)
        self.tokens.fallback_file.write_text(by_hand)  # type: ignore[union-attr]
        self.assertEqual(self.tokens.token(), stored)

    def test_a_token_put_by_hand_stands_in_for_an_expired_one(self) -> None:
        by_hand = _jwt(3600)
        _store(self.tokens.store_file, _jwt(-60))
        self.tokens.fallback_file.write_text(by_hand + "\n")  # type: ignore[union-attr]
        self.assertEqual(self.tokens.token(), by_hand)

    def test_an_expired_token_is_reported_and_not_used(self) -> None:
        _store(self.tokens.store_file, _jwt(-60))
        self.assertEqual(self.tokens.token(), "")
        status = self.tokens.status()
        self.assertEqual(status["mode"], "anonymous")
        self.assertTrue(status["expired"])
        self.assertIsNotNone(status["expires_at"])

    def test_a_saved_token_is_in_fastf1s_format(self) -> None:
        from fastf1.internals.f1account.tokens import TokenStore

        token = _jwt(3600)
        self.tokens.save(token)
        self.assertEqual(TokenStore(self.tokens.store_file).load().value, token)  # type: ignore[union-attr]
        self.assertEqual(self.tokens.token(), token)

    def test_the_status_never_carries_the_token_or_the_password(self) -> None:
        token = _jwt(3600)
        _store(self.tokens.store_file, token)
        auth = F1TVAuth(self.tokens, username="me@example.com", password="secret")
        status = json.dumps(auth.status())
        self.assertNotIn(token, status)
        self.assertNotIn("secret", status)
        self.assertEqual(auth.status()["mode"], "f1tv")
        self.assertTrue(auth.status()["sign_in"]["enabled"])


class _Stop(Exception):
    pass


class RenewalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.tokens = F1TVTokens(self.dir / "store.json")
        self.auth = F1TVAuth(self.tokens, username="me@example.com", password="secret")

    def test_no_credentials_never_sign_in(self) -> None:
        auth = F1TVAuth(self.tokens)
        auth.request()
        self.assertFalse(auth.due())

    def test_due_without_a_token_or_close_to_its_end(self) -> None:
        self.assertTrue(self.auth.due())
        _store(self.tokens.store_file, _jwt(3600))
        self.assertTrue(self.auth.due())
        _store(self.tokens.store_file, _jwt(2 * 86400))
        self.assertFalse(self.auth.due())

    def test_not_again_within_the_interval(self) -> None:
        self.auth.last_sign_in = time.time() - 60
        self.assertFalse(self.auth.due())
        self.auth.last_sign_in = time.time() - f1tv.SIGN_IN_INTERVAL_SECONDS - 1
        self.assertTrue(self.auth.due())

    def test_a_weekend_asks_for_a_sign_in_whatever_the_expiry(self) -> None:
        _store(self.tokens.store_file, _jwt(2 * 86400))
        self.assertFalse(self.auth.due())
        self.auth.request()
        self.assertTrue(self.auth.due())
        self.assertTrue(self.auth.status()["sign_in"]["pending"])

    def test_a_sign_in_that_has_just_gone_through_is_enough(self) -> None:
        _store(self.tokens.store_file, _jwt(4 * 86400))
        self.auth.last_sign_in = time.time() - 60
        self.auth.request()
        self.assertFalse(self.auth.status()["sign_in"]["pending"])
        self.auth.last_sign_in = time.time() - f1tv.SIGN_IN_INTERVAL_SECONDS - 1
        self.assertFalse(self.auth.due())

    def _renew(self, sign_in: mock.AsyncMock) -> None:
        with mock.patch.object(f1tv, "_sign_in_elsewhere", sign_in):
            asyncio.run(self.auth.renew(["socks5h://proxy:1080"]))

    def test_a_sign_in_is_kept_for_everybody(self) -> None:
        token = _jwt(4 * 86400)
        sign_in = mock.AsyncMock(return_value=token)
        self.auth.request()
        self._renew(sign_in)
        sign_in.assert_awaited_once_with("me@example.com", "secret", ["socks5h://proxy:1080"])
        self.assertEqual(self.tokens.token(), token)
        self.assertIsNone(self.auth.last_error)
        self.assertFalse(self.auth.status()["sign_in"]["pending"])
        self.auth.last_sign_in = time.time() - f1tv.SIGN_IN_INTERVAL_SECONDS - 1
        self.assertFalse(self.auth.due())

    def test_a_failed_sign_in_keeps_the_old_token_and_the_request(self) -> None:
        old = _jwt(2 * 86400)
        _store(self.tokens.store_file, old)
        self.auth.request()
        self._renew(mock.AsyncMock(side_effect=RuntimeError("bot check")))
        self.assertEqual(self.tokens.token(), old)
        self.assertIn("bot check", self.auth.status()["sign_in"]["last_error"])
        self.assertFalse(self.auth.due())  # waits for the interval
        self.auth.last_sign_in = time.time() - f1tv.SIGN_IN_INTERVAL_SECONDS - 1
        self.assertTrue(self.auth.due())  # and then tries again for the weekend


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def _run(self, auth: F1TVAuth) -> mock.AsyncMock:
        renew = mock.AsyncMock()
        with (
            mock.patch.object(auth, "renew", renew),
            mock.patch.object(f1tv.asyncio, "wait_for", mock.AsyncMock(side_effect=_Stop)),
            self.assertRaises(_Stop),
        ):
            asyncio.run(auth.run())
        return renew

    def test_the_server_waits_for_a_tunnel(self) -> None:
        proxies: list[str] = []
        auth = F1TVAuth(
            F1TVTokens(self.dir / "store.json"), "me", "secret", require_proxy=True, proxy_urls=lambda: proxies
        )
        self._run(auth).assert_not_called()
        self.assertIn("proxy", auth.status()["sign_in"]["last_error"])
        self.assertTrue(auth.due())  # the wait is not an attempt: the tunnel's return is used at once
        proxies.append("socks5h://gw:1080")
        self._run(auth).assert_awaited_once_with(["socks5h://gw:1080"])

    def test_without_the_requirement_it_signs_in_directly(self) -> None:
        auth = F1TVAuth(F1TVTokens(self.dir / "store.json"), "me", "secret")
        self._run(auth).assert_awaited_once_with([])

    def test_a_request_wakes_the_loop(self) -> None:
        tokens = F1TVTokens(self.dir / "store.json")
        _store(tokens.store_file, _jwt(2 * 86400))
        auth = F1TVAuth(tokens, "me", "secret")
        signed_in: list[list[str]] = []

        async def renew(proxies: list[str]) -> None:
            signed_in.append(proxies)
            auth._wanted = False

        async def scenario() -> None:
            with mock.patch.object(auth, "renew", renew), mock.patch.object(f1tv, "CHECK_INTERVAL_SECONDS", 3600):
                loop = asyncio.create_task(auth.run())
                await asyncio.sleep(0.05)
                self.assertEqual(signed_in, [])  # the token is good for two days
                auth.request()
                await asyncio.sleep(0.05)
                loop.cancel()

        asyncio.run(scenario())
        self.assertEqual(signed_in, [[]])


class SignInTests(unittest.TestCase):
    def _sign_in(self, *errors: Exception) -> tuple[list[str | None], object]:
        from fastf1.internals.f1account.tokens import SubscriptionToken

        # the package re-exports the function under the module's name
        login_module = importlib.import_module("fastf1.internals.f1account.login")
        tried: list[str | None] = []
        outcomes = list(errors)

        def login(credentials: object) -> SubscriptionToken:
            tried.append(os.environ.get("HTTPS_PROXY"))
            if outcomes:
                raise outcomes.pop(0)
            return SubscriptionToken.from_jwt(_jwt(3600))

        with mock.patch.object(login_module, "login", login), mock.patch.dict(os.environ):
            try:
                result: object = f1tv._sign_in("me", "secret", ["socks5h://gw:1080", "socks5h://gw:1081"])
            except Exception as exc:  # noqa: BLE001
                result = exc
        return tried, result

    def test_an_unreachable_proxy_hands_over_to_the_next(self) -> None:
        import requests
        from fastf1.exceptions import F1AuthError

        unreachable = F1AuthError("Sign-in failed")
        unreachable.__cause__ = requests.ConnectionError("tunnel down")
        tried, result = self._sign_in(unreachable)
        self.assertEqual(len(tried), 2)
        self.assertNotEqual(tried[0], tried[1])
        self.assertIsInstance(result, str)

    def test_a_refused_password_is_not_offered_again(self) -> None:
        from fastf1.exceptions import F1AuthError

        tried, result = self._sign_in(F1AuthError("Sign-in was rejected. Check username and password."))
        self.assertEqual(len(tried), 1)
        self.assertIsInstance(result, F1AuthError)


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        _keep_fastf1_token_manager(self)
        self.dir = Path(tempfile.mkdtemp())

    def test_fastf1_reads_the_kept_token(self) -> None:
        from fastf1.internals.f1account import get_token

        token = _jwt(3600)
        _store(self.dir / "store.json", token)
        F1TVTokens(self.dir / "store.json").install()
        self.assertEqual(get_token().value, token)

    def test_fastf1_never_signs_in_by_itself(self) -> None:
        from fastf1.exceptions import F1AuthError
        from fastf1.internals.f1account import get_token

        F1TVTokens(self.dir / "store.json").install()
        login_module = importlib.import_module("fastf1.internals.f1account.login")
        with (
            mock.patch.dict(os.environ, {"F1_USERNAME": "me@example.com", "F1_PASSWORD": "secret"}),
            mock.patch.object(login_module, "login") as login,
            mock.patch("builtins.input", side_effect=AssertionError("prompted")),
        ):
            with self.assertRaises(F1AuthError):
                get_token()
        login.assert_not_called()

    def test_fastf1_falls_back_to_the_token_put_by_hand(self) -> None:
        from fastf1.internals.f1account import get_token

        by_hand = _jwt(3600)
        (self.dir / "f1auth.json").write_text(by_hand)
        F1TVTokens(self.dir / "store.json", self.dir / "f1auth.json").install()
        self.assertEqual(get_token().value, by_hand)

    def test_what_goes_to_the_other_processes_holds_no_password(self) -> None:
        import pickle

        auth = F1TVAuth(F1TVTokens(self.dir / "store.json"), "me@example.com", "secret")
        self.assertNotIn(b"secret", pickle.dumps(auth.tokens))
        self.assertNotIn(b"secret", pickle.dumps(auth.tokens.install))


if __name__ == "__main__":
    unittest.main()
