import asyncio
import base64
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from xian.dashboard import cli
from xian.dashboard.app import (
    DashboardConcurrencyLimiter,
    DashboardRateLimiter,
    SubscriptionManager,
    _allowed_rpc_urls,
    _dashboard_listen_url,
    _decode_block_tx_entry,
    _localnet_rpc_variants,
    _normalize_peer_rpc_url,
    _prune_closed_dashboard_ws_clients,
    _resolved_rpc_url_variants,
    create_app,
    dashboard_security_middleware,
    handle_addresses,
    handle_contract,
    handle_index,
    handle_validator_dashboard,
    handle_ws,
    normalize_rpc_url,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def dashboard_request(
    path: str,
    *,
    remote: str = "203.0.113.10",
    app: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        path=path,
        remote=remote,
        transport=None,
        app=app if app is not None else {},
    )


class DashboardTests(unittest.TestCase):
    def test_normalize_rpc_url_accepts_http_and_laddr(self) -> None:
        self.assertEqual(
            normalize_rpc_url("http://127.0.0.1:26657/"),
            "http://127.0.0.1:26657",
        )
        self.assertEqual(
            normalize_rpc_url("tcp://127.0.0.1:26657"),
            "http://127.0.0.1:26657",
        )
        self.assertEqual(
            normalize_rpc_url("tcp://::1:26657"),
            "http://[::1]:26657",
        )
        self.assertEqual(
            normalize_rpc_url("http://::1:8080"),
            "http://[::1]:8080",
        )

    def test_subscription_manager_matches_state_and_event_filters(self) -> None:
        manager = SubscriptionManager()
        state_client = object()
        event_client = object()
        wildcard_client = object()

        manager.add_client(state_client)
        manager.add_client(event_client)
        manager.add_client(wildcard_client)

        manager.handle_message(
            state_client,
            {
                "action": "subscribe",
                "type": "state",
                "key": "currency.balances:*",
            },
        )
        manager.handle_message(
            event_client,
            {
                "action": "subscribe",
                "type": "event",
                "contract": "currency",
                "event": "Transfer",
            },
        )
        manager.handle_message(
            wildcard_client,
            {
                "action": "subscribe",
                "type": "event",
                "contract": "*",
            },
        )

        self.assertEqual(
            manager.match_state("currency.balances:alice"),
            [state_client],
        )
        self.assertEqual(
            set(manager.match_event("currency", "Transfer")),
            {event_client, wildcard_client},
        )

    def test_subscription_manager_enforces_per_client_limits(self) -> None:
        manager = SubscriptionManager(
            max_state_subscriptions_per_client=1,
            max_event_subscriptions_per_client=1,
        )
        client = object()
        manager.add_client(client)

        first_state = manager.handle_message(
            client,
            {
                "action": "subscribe",
                "type": "state",
                "key": "currency.balances:*",
            },
        )
        second_state = manager.handle_message(
            client,
            {
                "action": "subscribe",
                "type": "state",
                "key": "currency.balances:bob",
            },
        )
        first_event = manager.handle_message(
            client,
            {
                "action": "subscribe",
                "type": "event",
                "contract": "currency",
            },
        )
        second_event = manager.handle_message(
            client,
            {
                "action": "subscribe",
                "type": "event",
                "contract": "rewards",
            },
        )

        self.assertEqual(first_state["status"], "ok")
        self.assertEqual(second_state["status"], "error")
        self.assertIn("limit reached", second_state["message"])
        self.assertEqual(first_event["status"], "ok")
        self.assertEqual(second_event["status"], "error")
        self.assertIn("limit reached", second_event["message"])

    def test_dashboard_rate_limiter_limits_default_api_by_client(self) -> None:
        clock = FakeClock()
        limiter = DashboardRateLimiter(
            rest_rate_limit_per_second=1,
            rest_rate_limit_burst=1,
            clock=clock,
        )
        request = dashboard_request("/api/status")

        self.assertIsNone(limiter.check(request))
        limited = limiter.check(request)

        self.assertIsNotNone(limited)
        self.assertEqual(limited.status, 429)
        self.assertEqual(limited.headers["Retry-After"], "1")
        self.assertEqual(
            json.loads(limited.text)["error"],
            "dashboard rate limit exceeded",
        )
        self.assertIsNone(limiter.check(dashboard_request("/api/status", remote="203.0.113.11")))

        clock.advance(1.0)
        self.assertIsNone(limiter.check(request))

    def test_dashboard_rate_limiter_shares_expensive_route_bucket(self) -> None:
        clock = FakeClock()
        limiter = DashboardRateLimiter(
            rest_rate_limit_per_second=100,
            rest_rate_limit_burst=100,
            expensive_rest_rate_limit_per_second=1,
            expensive_rest_rate_limit_burst=1,
            clock=clock,
        )

        self.assertIsNone(limiter.check(dashboard_request("/api/contracts")))
        limited = limiter.check(dashboard_request("/api/abci_query/foo"))

        self.assertIsNotNone(limited)
        self.assertEqual(limited.status, 429)
        self.assertIsNone(limiter.check(dashboard_request("/api/config")))

    def test_dashboard_rate_limiter_ignores_static_routes(self) -> None:
        limiter = DashboardRateLimiter(
            rest_rate_limit_per_second=1,
            rest_rate_limit_burst=1,
        )

        self.assertIsNone(limiter.check(dashboard_request("/")))
        self.assertEqual(limiter.bucket_count, 0)

    def test_dashboard_rate_limiter_prunes_stale_keys(self) -> None:
        clock = FakeClock()
        limiter = DashboardRateLimiter(
            rest_rate_limit_per_second=1,
            rest_rate_limit_burst=1,
            max_keys=2,
            clock=clock,
        )

        self.assertIsNone(limiter.check(dashboard_request("/api/status", remote="203.0.113.1")))
        self.assertIsNone(limiter.check(dashboard_request("/api/status", remote="203.0.113.2")))
        self.assertEqual(limiter.bucket_count, 2)

        clock.advance(3601)
        self.assertIsNone(limiter.check(dashboard_request("/api/status", remote="203.0.113.3")))
        self.assertEqual(limiter.bucket_count, 1)

    def test_dashboard_concurrency_limiter_rejects_above_limit(self) -> None:
        limiter = DashboardConcurrencyLimiter(1)

        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        limiter.release()
        self.assertTrue(limiter.try_acquire())

    def test_create_app_installs_security_controls(self) -> None:
        app = create_app(
            "http://127.0.0.1:26657",
            max_ws_clients_per_client=6,
            max_rest_concurrency=7,
            rest_rate_limit_per_second=11,
            rest_rate_limit_burst=12,
            expensive_rest_rate_limit_per_second=3,
            expensive_rest_rate_limit_burst=4,
            rate_limit_max_keys=5,
        )

        self.assertIn(dashboard_security_middleware, app.middlewares)
        self.assertEqual(app["max_ws_clients_per_client"], 6)
        self.assertIsInstance(app["rate_limiter"], DashboardRateLimiter)
        self.assertIsInstance(
            app["rest_concurrency_limiter"],
            DashboardConcurrencyLimiter,
        )
        self.assertEqual(app["rest_concurrency_limiter"].limit, 7)

    def test_create_app_routes_favicon_png(self) -> None:
        app = create_app("http://127.0.0.1:26657")

        paths = {route.resource.canonical for route in app.router.routes()}

        self.assertIn("/favicon.png", paths)
        self.assertNotIn("/favicon.svg", paths)

    def test_decode_block_tx_entry_attaches_canonical_tx_hash(self) -> None:
        tx = {
            "payload": {
                "sender": "alice",
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"amount": 1},
            },
            "metadata": {"signature": "deadbeef"},
        }
        raw_json = json.dumps(tx, separators=(",", ":")).encode("utf-8")
        raw_hex = raw_json.hex().encode("utf-8")
        raw_b64 = base64.b64encode(raw_hex).decode("utf-8")

        decoded = _decode_block_tx_entry(raw_b64)

        self.assertEqual(decoded["payload"]["contract"], "currency")
        self.assertEqual(
            decoded["tx_hash"],
            hashlib.sha256(base64.b64decode(raw_b64)).hexdigest().upper(),
        )

    def test_normalize_peer_rpc_url_uses_remote_ip_for_wildcard_host(
        self,
    ) -> None:
        peer = {
            "remote_ip": "10.0.0.25",
            "node_info": {
                "other": {
                    "rpc_address": "tcp://0.0.0.0:26657",
                }
            },
        }

        self.assertEqual(
            _normalize_peer_rpc_url(peer),
            "http://10.0.0.25:26657",
        )

    def test_normalize_peer_rpc_url_brackets_ipv6_remote_ip(self) -> None:
        peer = {
            "remote_ip": "2001:db8::25",
            "node_info": {
                "other": {
                    "rpc_address": "tcp://[::]:26657",
                }
            },
        }

        self.assertEqual(
            _normalize_peer_rpc_url(peer),
            "http://[2001:db8::25]:26657",
        )

    def test_normalize_peer_rpc_url_accepts_unbracketed_ipv6_rpc_address(
        self,
    ) -> None:
        peer = {
            "node_info": {
                "other": {
                    "rpc_address": "tcp://::1:26657",
                }
            },
        }

        self.assertEqual(
            _normalize_peer_rpc_url(peer),
            "http://[::1]:26657",
        )

    def test_localnet_rpc_variants_infer_host_ports(self) -> None:
        self.assertEqual(
            _localnet_rpc_variants(
                "http://127.0.0.1:26657",
                "node-0",
                "node-2",
            ),
            {
                "http://127.0.0.1:26857",
                "http://localhost:26857",
            },
        )

    def test_localnet_rpc_variants_preserve_ipv6_loopback_family(self) -> None:
        self.assertEqual(
            _localnet_rpc_variants(
                "http://[::1]:26657",
                "node-0",
                "node-1",
            ),
            {
                "http://[::1]:26757",
                "http://localhost:26757",
            },
        )

    def test_resolved_rpc_url_variants_include_dns_aliases(self) -> None:
        with patch(
            "xian.dashboard.app.socket.getaddrinfo",
            return_value=[(0, 0, 0, "", ("172.20.0.7", 26657))],
        ):
            variants = _resolved_rpc_url_variants("http://node-0:26657")

        self.assertEqual(variants, {"http://172.20.0.7:26657"})

    def test_resolved_rpc_url_variants_bracket_ipv6_dns_aliases(self) -> None:
        with patch(
            "xian.dashboard.app.socket.getaddrinfo",
            return_value=[(0, 0, 0, "", ("2001:db8::7", 26657, 0, 0))],
        ):
            variants = _resolved_rpc_url_variants("http://node-0:26657")

        self.assertEqual(variants, {"http://[2001:db8::7]:26657"})

    def test_dashboard_listen_url_brackets_ipv6_hosts(self) -> None:
        self.assertEqual(_dashboard_listen_url("::", 8080), "http://[::]:8080")

    def test_allowed_rpc_urls_include_localnet_host_variants(self) -> None:
        async def run_test() -> None:
            async def fake_raw_rpc(_session, _rpc_url, path, params=None):
                del params
                if path == "status":
                    return {"node_info": {"moniker": "node-0"}}
                if path == "net_info":
                    return {
                        "peers": [
                            {
                                "remote_ip": "172.20.0.6",
                                "node_info": {
                                    "moniker": "node-1",
                                    "other": {"rpc_address": "tcp://0.0.0.0:26657"},
                                },
                            }
                        ]
                    }
                raise AssertionError(f"unexpected path: {path}")

            with patch("xian.dashboard.app._raw_rpc", side_effect=fake_raw_rpc):
                allowed = await _allowed_rpc_urls(
                    object(),
                    "http://127.0.0.1:26657",
                )

            self.assertIn("http://127.0.0.1:26757", allowed)
            self.assertIn("http://localhost:26757", allowed)

        import asyncio

        asyncio.run(run_test())

    def test_allowed_rpc_urls_include_default_dns_aliases(self) -> None:
        async def run_test() -> None:
            async def fake_raw_rpc(_session, _rpc_url, path, params=None):
                del params
                if path == "status":
                    return {"node_info": {"moniker": "node-0"}}
                if path == "net_info":
                    return {"peers": []}
                raise AssertionError(f"unexpected path: {path}")

            with (
                patch("xian.dashboard.app._raw_rpc", side_effect=fake_raw_rpc),
                patch(
                    "xian.dashboard.app.socket.getaddrinfo",
                    return_value=[(0, 0, 0, "", ("172.20.0.7", 26657))],
                ),
            ):
                allowed = await _allowed_rpc_urls(
                    object(),
                    "http://node-0:26657",
                )

            self.assertIn("http://172.20.0.7:26657", allowed)

        import asyncio

        asyncio.run(run_test())

    def test_cli_main_runs_dashboard_app(self) -> None:
        with (
            patch("xian.dashboard.cli.create_app", return_value=object()) as create_app,
            patch("xian.dashboard.cli.web.run_app") as run_app,
        ):
            exit_code = cli.main(
                [
                    "--rpc-url",
                    "http://127.0.0.1:26657",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18080",
                    "--max-ws-clients",
                    "24",
                    "--max-ws-clients-per-client",
                    "6",
                    "--max-state-subs-per-client",
                    "10",
                    "--max-event-subs-per-client",
                    "8",
                    "--max-ws-message-bytes",
                    "4096",
                    "--max-ws-outbound-queue",
                    "32",
                    "--rest-rate-limit-per-second",
                    "11.5",
                    "--rest-rate-limit-burst",
                    "12",
                    "--expensive-rest-rate-limit-per-second",
                    "3.5",
                    "--expensive-rest-rate-limit-burst",
                    "4",
                    "--max-rest-concurrency",
                    "5",
                    "--rate-limit-max-keys",
                    "6",
                ]
            )

        self.assertEqual(exit_code, 0)
        create_app.assert_called_once_with(
            "http://127.0.0.1:26657",
            max_ws_clients=24,
            max_ws_clients_per_client=6,
            max_state_subscriptions_per_client=10,
            max_event_subscriptions_per_client=8,
            max_ws_message_bytes=4096,
            max_ws_outbound_queue=32,
            rest_rate_limit_per_second=11.5,
            rest_rate_limit_burst=12,
            expensive_rest_rate_limit_per_second=3.5,
            expensive_rest_rate_limit_burst=4,
            max_rest_concurrency=5,
            rate_limit_max_keys=6,
        )
        run_app.assert_called_once()


class DashboardRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_index_disables_browser_cache(self) -> None:
        response = await handle_index(dashboard_request("/"))

        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.headers["Expires"], "0")

    async def test_security_middleware_rejects_rate_limited_request(
        self,
    ) -> None:
        clock = FakeClock()
        limiter = DashboardRateLimiter(
            rest_rate_limit_per_second=1,
            rest_rate_limit_burst=1,
            clock=clock,
        )
        request = dashboard_request(
            "/api/status",
            app={"rate_limiter": limiter},
        )
        calls = 0

        async def handler(_request):
            nonlocal calls
            calls += 1
            return web.Response(text="ok")

        allowed = await dashboard_security_middleware(request, handler)
        limited = await dashboard_security_middleware(request, handler)

        self.assertEqual(allowed.status, 200)
        self.assertEqual(limited.status, 429)
        self.assertEqual(calls, 1)

    async def test_security_middleware_rejects_when_rest_concurrency_full(
        self,
    ) -> None:
        concurrency = DashboardConcurrencyLimiter(1)
        self.assertTrue(concurrency.try_acquire())
        request = dashboard_request(
            "/api/status",
            app={
                "rate_limiter": None,
                "rest_concurrency_limiter": concurrency,
            },
        )
        calls = 0

        async def handler(_request):
            nonlocal calls
            calls += 1
            return web.Response(text="ok")

        response = await dashboard_security_middleware(request, handler)

        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertEqual(calls, 0)
        concurrency.release()

    async def test_security_middleware_releases_rest_concurrency(
        self,
    ) -> None:
        concurrency = DashboardConcurrencyLimiter(1)
        request = dashboard_request(
            "/api/status",
            app={
                "rate_limiter": None,
                "rest_concurrency_limiter": concurrency,
            },
        )

        async def handler(_request):
            self.assertEqual(concurrency.active, 1)
            return web.Response(text="ok")

        response = await dashboard_security_middleware(request, handler)

        self.assertEqual(response.status, 200)
        self.assertEqual(concurrency.active, 0)
        self.assertTrue(concurrency.try_acquire())

    async def test_security_middleware_does_not_gate_websocket_concurrency(
        self,
    ) -> None:
        concurrency = DashboardConcurrencyLimiter(1)
        self.assertTrue(concurrency.try_acquire())
        request = dashboard_request(
            "/ws",
            app={
                "rate_limiter": None,
                "rest_concurrency_limiter": concurrency,
            },
        )

        async def handler(_request):
            return web.Response(text="ok")

        response = await dashboard_security_middleware(request, handler)

        self.assertEqual(response.status, 200)
        concurrency.release()

    async def test_handle_ws_rejects_connections_when_client_limit_reached(
        self,
    ) -> None:
        request = SimpleNamespace(
            app={
                "ws_clients": {object()},
                "max_ws_clients": 1,
            }
        )

        with self.assertRaises(web.HTTPServiceUnavailable):
            await handle_ws(request)

    async def test_handle_ws_rejects_connections_when_per_client_limit_reached(
        self,
    ) -> None:
        request = dashboard_request(
            "/ws",
            app={
                "ws_clients": set(),
                "ws_client_counts": {"203.0.113.10": 1},
                "max_ws_clients": 10,
                "max_ws_clients_per_client": 1,
            },
        )

        with self.assertRaises(web.HTTPServiceUnavailable):
            await handle_ws(request)

    async def test_prune_closed_dashboard_ws_clients_releases_client_slot(
        self,
    ) -> None:
        class FakeWebSocket:
            def __init__(self, *, closed: bool):
                self.closed = closed

        closed_ws = FakeWebSocket(closed=True)
        open_ws = FakeWebSocket(closed=False)
        sender_task = asyncio.create_task(asyncio.sleep(0))
        await sender_task
        app = {
            "ws_clients": {closed_ws, open_ws},
            "ws_client_counts": {"203.0.113.10": 2},
            "ws_client_states": {
                closed_ws: SimpleNamespace(
                    client_key="203.0.113.10",
                    sender_task=sender_task,
                )
            },
            "subscriptions": SubscriptionManager(),
        }
        app["subscriptions"].add_client(closed_ws)
        app["subscriptions"].add_client(open_ws)

        await _prune_closed_dashboard_ws_clients(app)

        self.assertEqual(app["ws_clients"], {open_ws})
        self.assertEqual(app["ws_client_counts"], {"203.0.113.10": 1})
        self.assertNotIn(closed_ws, app["ws_client_states"])

    async def test_handle_contract_returns_source_only(
        self,
    ) -> None:
        request = SimpleNamespace(
            match_info={"name": "currency"},
            query={},
            app={"session": object(), "rpc_url": "http://127.0.0.1:26657"},
        )

        async def fake_abci_query(_session, _rpc, path):
            responses = {
                "contract_source/currency": "@export\ndef ping():\n    return 'pong'\n",
                "contract_methods/currency": [{"name": "ping"}],
                "contract_vars/currency": [],
                "contract_info/currency": {"developer": "alice"},
                "contract_summary/currency": {"tx_count": 1},
            }
            return responses.get(path)

        with patch(
            "xian.dashboard.app._abci_query",
            side_effect=fake_abci_query,
        ):
            response = await handle_contract(request)

        payload = json.loads(response.text)
        self.assertEqual(
            payload["source"],
            "@export\ndef ping():\n    return 'pong'\n",
        )
        self.assertNotIn("runtime_code", payload)
        self.assertNotIn("code", payload)
        self.assertTrue(payload["has_original_source"])

    async def test_handle_addresses_requires_indexed_address_query(self) -> None:
        request = SimpleNamespace(
            query={"limit": "10", "offset": "0"},
            app={"session": object(), "rpc_url": "http://127.0.0.1:26657"},
        )

        async def fake_abci_query(_session, _rpc, path):
            self.assertEqual(path, "addresses/limit=10/offset=0")
            return None

        with patch(
            "xian.dashboard.app._abci_query",
            side_effect=fake_abci_query,
        ):
            response = await handle_addresses(request)

        payload = json.loads(response.text)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["items"], [])
        self.assertFalse(payload["has_more"])

    async def test_handle_validator_dashboard_combines_validator_queries(
        self,
    ) -> None:
        validator_pubkey_hex = "ee06a34cf08bf72ce592d26d36b90c79daba2829ba9634992d034318160d49f9"
        request = SimpleNamespace(
            query={},
            app={"session": object(), "rpc_url": "http://127.0.0.1:26657"},
        )

        async def fake_raw_rpc(_session, _rpc, path, params=None):
            del params
            self.assertEqual(path, "status")
            return {
                "validator_info": {
                    "pub_key": {
                        "value": base64.b64encode(bytes.fromhex(validator_pubkey_hex)).decode(
                            "ascii"
                        )
                    }
                }
            }

        async def fake_abci_query(_session, _rpc, path):
            responses = {
                "validators_policy": {"selection_mode": "manual"},
                "validators_active": [{"account": validator_pubkey_hex}],
                "validators_candidates": [{"account": "candidate-1"}],
                "validators_open_votes/limit=25/offset=0": [
                    {"proposal_id": 7, "type": "update_policy"}
                ],
                f"validators_validator/{validator_pubkey_hex}": {
                    "account": validator_pubkey_hex,
                    "status": "active",
                    "total_bond": 150,
                },
                f"validators_pending_unbonds/{validator_pubkey_hex}": [
                    {"unbond_id": 4, "amount": 25}
                ],
            }
            return responses.get(path)

        with (
            patch("xian.dashboard.app._raw_rpc", side_effect=fake_raw_rpc),
            patch(
                "xian.dashboard.app._abci_query",
                side_effect=fake_abci_query,
            ),
        ):
            response = await handle_validator_dashboard(request)

        payload = json.loads(response.text)
        self.assertEqual(payload["local_account"], validator_pubkey_hex)
        self.assertEqual(payload["policy"]["selection_mode"], "manual")
        self.assertEqual(payload["active_validators"][0]["account"], validator_pubkey_hex)
        self.assertEqual(payload["pending_candidates"][0]["account"], "candidate-1")
        self.assertEqual(payload["open_votes"][0]["proposal_id"], 7)
        self.assertEqual(payload["local_validator"]["status"], "active")
        self.assertEqual(payload["local_pending_unbonds"][0]["unbond_id"], 4)
