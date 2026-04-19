import base64
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from xian.dashboard import cli
from xian.dashboard.app import (
    SubscriptionManager,
    _allowed_rpc_urls,
    _decode_block_tx_entry,
    _localnet_rpc_variants,
    _normalize_peer_rpc_url,
    handle_addresses,
    handle_contract,
    handle_validator_dashboard,
    handle_ws,
    normalize_rpc_url,
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
                                    "other": {
                                        "rpc_address": "tcp://0.0.0.0:26657"
                                    },
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

    def test_cli_main_runs_dashboard_app(self) -> None:
        with (
            patch(
                "xian.dashboard.cli.create_app", return_value=object()
            ) as create_app,
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
                    "--max-state-subs-per-client",
                    "10",
                    "--max-event-subs-per-client",
                    "8",
                    "--max-ws-message-bytes",
                    "4096",
                    "--max-ws-outbound-queue",
                    "32",
                ]
            )

        self.assertEqual(exit_code, 0)
        create_app.assert_called_once_with(
            "http://127.0.0.1:26657",
            max_ws_clients=24,
            max_state_subscriptions_per_client=10,
            max_event_subscriptions_per_client=8,
            max_ws_message_bytes=4096,
            max_ws_outbound_queue=32,
        )
        run_app.assert_called_once()


class DashboardRouteTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_handle_contract_separates_source_from_runtime_code(
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
                "contract/currency": "def __ping():\n    return 'pong'\n",
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
        self.assertEqual(
            payload["runtime_code"],
            "def __ping():\n    return 'pong'\n",
        )
        self.assertTrue(payload["has_original_source"])
        self.assertEqual(payload["code"], payload["source"])

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
        validator_pubkey_hex = (
            "ee06a34cf08bf72ce592d26d36b90c79"
            "daba2829ba9634992d034318160d49f9"
        )
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
                        "value": base64.b64encode(
                            bytes.fromhex(validator_pubkey_hex)
                        ).decode("ascii")
                    }
                }
            }

        async def fake_abci_query(_session, _rpc, path):
            responses = {
                "masternodes_policy": {"selection_mode": "manual"},
                "masternodes_active": [{"account": validator_pubkey_hex}],
                "masternodes_candidates": [{"account": "candidate-1"}],
                "masternodes_open_votes/limit=25/offset=0": [
                    {"proposal_id": 7, "type": "update_policy"}
                ],
                f"masternodes_validator/{validator_pubkey_hex}": {
                    "account": validator_pubkey_hex,
                    "status": "active",
                    "total_bond": 150,
                },
                f"masternodes_pending_unbonds/{validator_pubkey_hex}": [
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
