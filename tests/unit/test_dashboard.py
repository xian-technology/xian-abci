import base64
import hashlib
import json
import unittest
from unittest.mock import patch

from xian.dashboard import cli
from xian.dashboard.app import (
    SubscriptionManager,
    _decode_block_tx_entry,
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
                ]
            )

        self.assertEqual(exit_code, 0)
        create_app.assert_called_once_with("http://127.0.0.1:26657")
        run_app.assert_called_once()
