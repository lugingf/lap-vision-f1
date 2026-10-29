import asyncio
import unittest

from app.services.proxy_health import ProxyHealth

PHONE = "socks5h://172.18.0.1:1081"
BOARD = "socks5h://172.18.0.1:1082"


class ProxyHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registered = [PHONE, BOARD]
        self.answers = {PHONE: (200, None), BOARD: (200, None)}
        self.health = ProxyHealth(proxy_urls=lambda: list(self.registered), probe=self.answers.__getitem__)

    def test_nothing_checked_yet_is_not_ok(self) -> None:
        status = self.health.status()

        self.assertFalse(status["ok"])
        self.assertIsNone(status["checked_at"])
        self.assertEqual(status["proxies"], [])

    def test_f1_is_reachable_while_any_proxy_gets_through(self) -> None:
        self.answers[PHONE] = (None, "ProxyError: connection refused")
        asyncio.run(self.health.check())

        status = self.health.status()
        by_url = {proxy["url"]: proxy for proxy in status["proxies"]}
        self.assertTrue(status["ok"])
        self.assertFalse(by_url[PHONE]["ok"])
        self.assertEqual(by_url[PHONE]["error"], "ProxyError: connection refused")
        self.assertIsNone(by_url[PHONE]["last_ok_at"])
        self.assertTrue(by_url[BOARD]["ok"])
        self.assertEqual(by_url[BOARD]["status_code"], 200)

    def test_a_refused_address_is_not_ok(self) -> None:
        """A tunnel that is up while F1 answers 403 is as good as no tunnel: that is what the check is for."""
        self.registered = [BOARD]
        self.answers[BOARD] = (403, None)
        asyncio.run(self.health.check())

        status = self.health.status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["proxies"][0]["status_code"], 403)

    def test_last_success_survives_a_failure(self) -> None:
        asyncio.run(self.health.check())
        first_ok = self.health.status()["proxies"][1]["last_ok_at"]
        self.answers[BOARD] = (None, "ReadTimeout")
        asyncio.run(self.health.check())

        board = self.health.status()["proxies"][1]
        self.assertFalse(board["ok"])
        self.assertEqual(board["last_ok_at"], first_ok)

    def test_a_proxy_taken_off_the_list_is_forgotten(self) -> None:
        asyncio.run(self.health.check())
        self.registered = [BOARD]
        asyncio.run(self.health.check())

        self.assertEqual([proxy["url"] for proxy in self.health.status()["proxies"]], [BOARD])


if __name__ == "__main__":
    unittest.main()
