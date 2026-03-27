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

    async def fetchrow(self, query: str, args: list[object]):
        self.fetchrow_calls.append((query, args))
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


if __name__ == "__main__":
    unittest.main()
