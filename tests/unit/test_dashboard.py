import unittest

from xian.dashboard.app import SubscriptionManager, normalize_rpc_url


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
