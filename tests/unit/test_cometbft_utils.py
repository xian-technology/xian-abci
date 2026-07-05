import unittest

from xian.utils.cometbft import normalize_rpc_url, resolve_local_rpc_url


class CometBftUtilsTests(unittest.TestCase):
    def test_normalize_rpc_url_accepts_http_and_laddr(self) -> None:
        self.assertEqual(
            normalize_rpc_url("http://127.0.0.1:26657/"),
            "http://127.0.0.1:26657",
        )
        self.assertEqual(
            normalize_rpc_url("tcp://127.0.0.1:26657"),
            "http://127.0.0.1:26657",
        )

    def test_normalize_rpc_url_brackets_ipv6_hosts(self) -> None:
        self.assertEqual(
            normalize_rpc_url("http://[::1]:26657/"),
            "http://[::1]:26657",
        )
        self.assertEqual(
            normalize_rpc_url("tcp://::1:26657"),
            "http://[::1]:26657",
        )
        self.assertEqual(
            normalize_rpc_url("::1:26657"),
            "http://[::1]:26657",
        )
        self.assertEqual(
            normalize_rpc_url("http://::1:8080"),
            "http://[::1]:8080",
        )

    def test_resolve_local_rpc_url_rewrites_wildcard_hosts(self) -> None:
        self.assertEqual(
            resolve_local_rpc_url("tcp://0.0.0.0:26657"),
            "http://127.0.0.1:26657",
        )
        self.assertEqual(
            resolve_local_rpc_url("http://[::]:26657"),
            "http://127.0.0.1:26657",
        )

    def test_resolve_local_rpc_url_can_resolve_ipv6_wildcard_to_ipv6_loopback(
        self,
    ) -> None:
        self.assertEqual(
            resolve_local_rpc_url("tcp://[::]:26657", default_ipv6_host="::1"),
            "http://[::1]:26657",
        )

    def test_resolve_local_rpc_url_brackets_ipv6_default_host(self) -> None:
        self.assertEqual(
            resolve_local_rpc_url("tcp://0.0.0.0:26657", default_host="::1"),
            "http://[::1]:26657",
        )


if __name__ == "__main__":
    unittest.main()
