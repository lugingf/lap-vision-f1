"""The F1TV subscription token: where it is kept, who signs in for it, and who uses it.

Positions and car data are sent only to a subscriber, and since mid-October 2026 the archive answers
nothing at all without the token. FastF1 signs in with the account's email and password (OAuth
through id.formula1.com, no browser) and is given a token good for about four days that cannot be
refreshed: once it runs out, the sign-in is repeated.

Only the service's own process signs in, and only through the tunnels to F1: F1 refuses the server's
address, for the sign-in as for everything else. It signs in before the first session of a weekend
is recorded, so that one token carries the whole weekend and nothing signs in while the cars are on
track, and otherwise only when the token is missing or about to run out. Everything else that talks
to F1 (the worker processes, the live recorder) reads the token kept in a file and never signs in by
itself: every process that finds the token expired signing in at once is a burst of logins F1 may
well take for a bot.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger("lap-vision-f1.f1tv")

# Outside the weekends, sign in again this long before the token runs out. The one a weekend's first
# session signs in for (about four days) runs out after the race, so this never falls on the weekend.
RENEW_BEFORE_SECONDS = 12 * 3600
# Not more often than this, successful or not: a refused password is not retried in a loop.
SIGN_IN_INTERVAL_SECONDS = 30 * 60
# The tunnels are registered every minute; a sign-in waiting for one starts within a minute of it.
CHECK_INTERVAL_SECONDS = 60


def _expiry(token: str) -> float | None:
    """The token's `exp`, read without verifying it: the feed does the verifying."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _is_valid(token: str, now: float) -> bool:
    expires = _expiry(token)
    return bool(token) and (expires is None or expires > now)


def _to_iso(epoch_seconds: float | None) -> str | None:
    return None if epoch_seconds is None else datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


@dataclass(frozen=True)
class F1TVTokens:
    """The token as every process reads it. Holds nothing but where it is: this is what is handed to
    the worker processes and the recorder, and the password stays out of them."""

    # Where the token the service signed in for is kept, in FastF1's own format.
    store_file: Path
    # A token somebody put there by hand (FastF1's browser login): used when the stored one is no good.
    fallback_file: Path | None = None

    def _stored(self) -> str:
        try:
            return str(json.loads(self.store_file.read_text())["value"])
        except (OSError, ValueError, KeyError, TypeError):
            return ""

    def _fallback(self) -> str:
        if self.fallback_file is None:
            return ""
        try:
            return self.fallback_file.read_text().strip()
        except OSError:
            return ""

    def token(self) -> str:
        """The token to connect with now, or an empty string for the anonymous feed. Never signs in."""
        now = time.time()
        for token in (self._stored(), self._fallback()):
            if _is_valid(token, now):
                return token
        return ""

    def stored_expiry(self) -> float | None:
        return _expiry(self._stored())

    def status(self) -> dict[str, Any]:
        """Whether there is a token and until when. Never the token itself."""
        token = self.token() or self._stored() or self._fallback()
        expires = _expiry(token) if token else None
        expired = bool(token) and not _is_valid(token, time.time())
        return {
            "mode": "f1tv" if token and not expired else "anonymous",
            "expires_at": _to_iso(expires),
            "expired": expired,
        }

    def save(self, token: str) -> None:
        from fastf1.internals.f1account.tokens import SubscriptionToken, TokenStore  # type: ignore

        TokenStore(self.store_file).save(SubscriptionToken.from_jwt(token))

    def install(self) -> None:
        """Point FastF1, in this process, at the token kept here, and take the signing in away from it.

        Left alone, FastF1 keeps its token in the user's home and, finding none, signs in with
        F1_USERNAME / F1_PASSWORD - or asks for them on a terminal, which in a server is a request that
        never returns. FastF1 has no setting for either, so its token manager is pointed here by hand."""
        from fastf1.exceptions import F1AuthError  # type: ignore
        from fastf1.internals.f1account import token_manager  # type: ignore
        from fastf1.internals.f1account.tokens import SubscriptionToken, TokenStore  # type: ignore

        def from_fallback() -> SubscriptionToken:
            token = self._fallback()
            if not _is_valid(token, time.time()):
                raise F1AuthError("no F1TV token yet: the service signs in, this process only reads it")
            return SubscriptionToken.from_jwt(token)

        token_manager._store = TokenStore(self.store_file)
        token_manager._authenticate = from_fallback
        token_manager._token = None


def _sign_in(username: str, password: str, proxy_urls: list[str]) -> str:
    """Runs in a process of its own: the proxy goes into its environment, which the sign-in follows.

    The next proxy is tried only when this one did not get through to F1. A refused password is
    refused through every proxy alike, and offering it again through each is a row of failed logins
    on the account."""
    import requests
    from fastf1.internals.f1account.login import Credentials, login  # type: ignore

    from app.services.historical import _apply_proxy_env, _proxy_attempts

    unreachable: Exception | None = None
    for proxy_url in _proxy_attempts(proxy_urls):
        _apply_proxy_env(proxy_url)
        try:
            return login(Credentials(username, password)).value
        except Exception as exc:
            # FastF1 wraps what the network did in its own error; anything else is F1's answer
            if not isinstance(exc.__cause__, requests.RequestException):
                raise
            unreachable = exc
    assert unreachable is not None
    raise unreachable


async def _sign_in_elsewhere(username: str, password: str, proxy_urls: list[str]) -> str:
    """The sign-in in a short-lived process: not the workers', where it would queue behind a session
    load of several minutes, and not this one, whose environment the proxy must stay out of."""
    executor = ProcessPoolExecutor(max_workers=1)
    try:
        return await asyncio.get_running_loop().run_in_executor(executor, _sign_in, username, password, proxy_urls)
    finally:
        executor.shutdown(wait=False)


class F1TVAuth:
    """Signs in for the token, in the service's own process only."""

    def __init__(
        self,
        tokens: F1TVTokens,
        username: str = "",
        password: str = "",
        require_proxy: bool = False,
        proxy_urls: Callable[[], list[str]] = list,
    ) -> None:
        self.tokens = tokens
        self._username = username
        self._password = password
        # Sign in only through a proxy: on the server, F1 refuses its own address, and a sign-in tried
        # from there while the tunnel is down only puts off the one that would get through.
        self._require_proxy = require_proxy
        self._proxy_urls = proxy_urls
        self.last_sign_in: float | None = None
        self.last_error: str | None = None
        # A sign-in asked for whatever the token's expiry: before a weekend's first session.
        self._wanted = False
        self._wake: asyncio.Event | None = None

    @property
    def can_sign_in(self) -> bool:
        return bool(self._username and self._password)

    def status(self) -> dict[str, Any]:
        """What the screen and the admin are told: the token's status and how the signing in goes."""
        return {
            **self.tokens.status(),
            "sign_in": {
                "enabled": self.can_sign_in,
                "pending": self._wanted,
                "last_at": _to_iso(self.last_sign_in),
                "last_error": self.last_error,
            },
        }

    def due(self) -> bool:
        if not self.can_sign_in:
            return False
        now = time.time()
        if self.last_sign_in is not None and now - self.last_sign_in < SIGN_IN_INTERVAL_SECONDS:
            return False
        if self._wanted:
            return True
        expires = self.tokens.stored_expiry()
        return expires is None or expires - now < RENEW_BEFORE_SECONDS

    def request(self) -> None:
        """Sign in now, whatever the token's expiry: a weekend's first session is about to be recorded,
        and a token signed in for now outlasts the race. Not again after a sign-in that has just gone
        through: that token is as fresh as one signed in for now."""
        if not self.can_sign_in:
            return
        just_signed_in = (
            self.last_sign_in is not None
            and self.last_error is None
            and time.time() - self.last_sign_in < SIGN_IN_INTERVAL_SECONDS
        )
        if just_signed_in:
            return
        self._wanted = True
        if self._wake is not None:
            self._wake.set()

    async def renew(self, proxy_urls: list[str]) -> None:
        """Sign in and keep the token. A failure is kept for the status; the old token stays."""
        self.last_sign_in = time.time()
        try:
            token = await _sign_in_elsewhere(self._username, self._password, proxy_urls)
        except Exception as exc:  # noqa: BLE001 - whatever the reason, the next attempt is after the interval
            self.last_error = f"{type(exc).__name__}: {exc}"
            _logger.warning("f1tv: sign-in failed: %s", self.last_error)
            return
        self.tokens.save(token)
        self.last_error = None
        self._wanted = False
        _logger.info("f1tv: signed in, token valid until %s", _to_iso(_expiry(token)))

    async def run(self) -> None:
        """Sign in whenever it is due, checking every minute and at once when a sign-in is asked for."""
        self._wake = asyncio.Event()
        while True:
            self._wake.clear()
            if self.due():
                proxies = self._proxy_urls()
                if proxies or not self._require_proxy:
                    await self.renew(proxies)
                else:
                    self.last_error = "waiting for a proxy: no tunnel is registered"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), CHECK_INTERVAL_SECONDS)
