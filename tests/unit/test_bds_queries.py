import unittest
from datetime import UTC, datetime
from decimal import Decimal

from xian.services.bds import sql
from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig


class _FakeDb:
    def __init__(self, row):
        self.row = row
        self.fetchrow_calls: list[tuple[str, list[object]]] = []
        self.fetch_calls: list[tuple[str, list[object]]] = []

    async def fetchrow(self, query: str, args: list[object]):
        self.fetchrow_calls.append((query, args))
        return self.row

    async def fetch(self, query: str, args: list[object]):
        self.fetch_calls.append((query, args))
        return self.row


class BdsQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_developer_rewards_returns_aggregate_summary(self):
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            {
                "recipient_key": "alice",
                "total_rewards": Decimal("12.75"),
                "reward_count": 4,
                "tx_count": 3,
                "contract_count": 2,
                "first_block_height": 10,
                "last_block_height": 14,
                "first_reward_at": datetime(2026, 1, 1, tzinfo=UTC),
                "last_reward_at": datetime(2026, 1, 2, tzinfo=UTC),
            }
        )

        result = await bds.get_developer_rewards("alice")

        self.assertEqual(
            bds.db.fetchrow_calls,
            [(sql.select_developer_rewards_summary(), ["alice"])],
        )
        self.assertEqual(result["recipient_key"], "alice")
        self.assertEqual(result["total_rewards"], Decimal("12.75"))
        self.assertEqual(result["reward_count"], 4)
        self.assertEqual(result["tx_count"], 3)
        self.assertEqual(result["contract_count"], 2)

    async def test_get_contract_summary_returns_aggregate_summary(self):
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            {
                "name": "currency",
                "last_tx_hash": "TX-CREATE",
                "submitted_at_block": 12,
                "submitted_at": datetime(2026, 1, 1, tzinfo=UTC),
                "creator": "alice",
                "tx_count": 5,
                "total_rewards": Decimal("18.25"),
                "reward_count": 3,
                "first_block_height": 12,
                "last_block_height": 18,
                "first_reward_at": datetime(2026, 1, 1, tzinfo=UTC),
                "last_reward_at": datetime(2026, 1, 2, tzinfo=UTC),
            }
        )

        result = await bds.get_contract_summary("currency")

        self.assertEqual(
            bds.db.fetchrow_calls,
            [(sql.select_contract_summary(), ["currency"])],
        )
        self.assertEqual(result["name"], "currency")
        self.assertEqual(result["creator"], "alice")
        self.assertEqual(result["tx_count"], 5)
        self.assertEqual(result["total_rewards"], Decimal("18.25"))

    async def test_get_recent_addresses_returns_activity_rows(self):
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            [
                {
                    "address": "alice",
                    "tx_count": 3,
                    "last_block_height": 12,
                    "last_seen": datetime(2026, 1, 1, tzinfo=UTC),
                    "last_tx_hash": "TX-1",
                    "last_contract": "currency",
                    "last_function": "transfer",
                }
            ]
        )

        result = await bds.get_recent_addresses(limit=25, offset=10)

        self.assertEqual(
            bds.db.fetch_calls,
            [(sql.select_recent_addresses(), [25, 10])],
        )
        self.assertEqual(result[0]["address"], "alice")
        self.assertEqual(result[0]["tx_count"], 3)

    async def test_get_token_balances_returns_portfolio_rows(self):
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            [
                {
                    "contract": "currency",
                    "balance": "12.5",
                    "balance_numeric": Decimal("12.5"),
                    "last_tx_hash": "TX-1",
                    "last_block_height": 12,
                    "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "token_name": "Xian",
                    "token_symbol": "XIAN",
                    "token_logo_url": "https://example.com/xian.svg",
                    "total_count": 1,
                }
            ]
        )

        result = await bds.get_token_balances(
            "alice", limit=25, offset=10, include_zero=True
        )

        self.assertEqual(
            bds.db.fetch_calls,
            [(sql.select_token_balances(), ["alice", True, 25, 10])],
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["address"], "alice")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["contract"], "currency")
        self.assertEqual(result["items"][0]["balance"], "12.5")
        self.assertEqual(result["items"][0]["symbol"], "XIAN")


if __name__ == "__main__":
    unittest.main()
