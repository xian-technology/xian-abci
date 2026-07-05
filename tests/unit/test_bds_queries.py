import unittest
from datetime import UTC, datetime
from decimal import Decimal

from xian.services.bds import sql
from xian.services.bds.bds import BDS
from xian.services.bds.candles import get_candle_source_spec
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
    def test_transaction_selectors_expose_tx_hash_without_duplicate_hash_field(self):
        selectors = [
            sql.select_transaction_by_hash(),
            sql.select_transactions_for_block_height(),
            sql.select_transactions_for_block_hash(),
            sql.select_transactions_by_sender(),
            sql.select_transactions_by_contract(),
        ]

        for selector in selectors:
            with self.subTest(selector=selector):
                self.assertIn("hash AS tx_hash", selector)
                self.assertNotIn("hash,\n        hash AS tx_hash", selector)

    def test_index_status_counts_all_transactions(self):
        query = sql.select_index_status()

        self.assertIn("(SELECT COUNT(*) FROM transactions) AS indexed_tx_count", query)
        self.assertNotIn("SELECT tx_count FROM blocks", query)

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
                    "first_block_height": 10,
                    "first_seen": datetime(2026, 1, 1, tzinfo=UTC),
                    "last_block_height": 12,
                    "last_tx_index": 0,
                    "last_seen": datetime(2026, 1, 1, tzinfo=UTC),
                    "last_tx_hash": "TX-1",
                    "last_contract": "currency",
                    "last_function": "transfer",
                    "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
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

    async def test_get_token_contracts_returns_standard_token_rows(self):
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            [
                {
                    "contract": "currency",
                    "last_tx_hash": "TX-CREATE",
                    "submitted_at_block": 0,
                    "submitted_at": datetime(1970, 1, 1, tzinfo=UTC),
                    "token_name": "Xian",
                    "token_symbol": "XIAN",
                    "token_logo_url": "https://example.com/xian.svg",
                    "total_count": 1,
                }
            ]
        )

        result = await bds.get_token_contracts(limit=25, offset=10)

        self.assertEqual(
            bds.db.fetch_calls,
            [(sql.select_token_contracts(), [25, 10])],
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["contract"], "currency")
        self.assertEqual(result["items"][0]["symbol"], "XIAN")

    async def test_get_state_previous_returns_current_and_previous_value(self):
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            {
                "key": "currency.balances:alice",
                "current_value": "12",
                "last_change_id": 7,
                "last_tx_hash": "TX-7",
                "last_block_height": 12,
                "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
                "previous_change_id": 6,
                "previous_value": "10",
                "previous_tx_hash": "TX-6",
                "previous_block_height": 11,
            }
        )

        result = await bds.get_state_previous("currency.balances:alice")

        self.assertEqual(
            bds.db.fetchrow_calls,
            [(sql.select_state_previous(), ["currency.balances:alice"])],
        )
        self.assertEqual(result["current_value"], "12")
        self.assertEqual(result["previous_value"], "10")

    async def test_get_shielded_output_tags_returns_index_rows(self):
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            [
                {
                    "id": 1,
                    "tag_kind": "sync_hint",
                    "tag_value": "0x1234",
                    "commitment": "0x" + "11" * 32,
                    "block_height": 12,
                }
            ]
        )

        result = await bds.get_shielded_output_tags(
            "0x1234", kind="sync_hint", limit=25, offset=10
        )

        self.assertEqual(
            bds.db.fetch_calls,
            [(sql.select_shielded_output_tags(), ["sync_hint", "0x1234", 25, 10])],
        )
        self.assertEqual(result[0]["tag_value"], "0x1234")
        self.assertEqual(result[0]["tag_kind"], "sync_hint")
        self.assertEqual(result[0]["block_height"], 12)

    async def test_get_dex_candles_returns_server_side_ohlcv_buckets(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        bds = BDS(BdsConfig())
        bds.db = _FakeDb(
            [
                {
                    "pair_id": 7,
                    "source": "xian_pairs_v1",
                    "market_id": "7",
                    "bucket_start": datetime(2026, 1, 1, 12, tzinfo=UTC),
                    "bucket_end": datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
                    "open": Decimal("1.0"),
                    "high": Decimal("1.2"),
                    "low": Decimal("0.9"),
                    "close": Decimal("1.1"),
                    "volume_token0": Decimal("42"),
                    "volume_token1": Decimal("46.2"),
                    "trade_count": 3,
                    "first_event_id": 10,
                    "last_event_id": 12,
                }
            ]
        )

        result = await bds.get_dex_candles(
            7,
            source_spec=get_candle_source_spec(),
            interval_seconds=300,
            limit=25,
            offset=5,
            start_time=start,
            end_time=end,
        )

        self.assertEqual(
            bds.db.fetch_calls,
            [
                (
                    sql.select_dex_candles(),
                    [
                        "con_pairs",
                        "Swap",
                        "xian_pairs_v1",
                        "pair",
                        "7",
                        7,
                        start,
                        end,
                        300,
                        25,
                        5,
                        "amount0In",
                        "amount1In",
                        "amount0Out",
                        "amount1Out",
                    ],
                )
            ],
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "xian_pairs_v1")
        self.assertEqual(result["market_id"], "7")
        self.assertEqual(result["pair_id"], 7)
        self.assertEqual(result["interval_seconds"], 300)
        self.assertEqual(result["items"][0]["open"], Decimal("1.0"))

    async def test_get_shielded_wallet_history_returns_commitments_with_optional_payloads(self):
        class _ShieldedHistoryDb:
            def __init__(self):
                self.fetch_calls: list[tuple[str, list[object]]] = []

            async def fetch(self, query: str, args: list[object]):
                self.fetch_calls.append((query, args))
                if query == sql.select_shielded_wallet_history():
                    return [
                        {
                            "output_id": 21,
                            "event_id": None,
                            "block_height": 12,
                            "tx_hash": "TX-1",
                            "tx_index": 0,
                            "contract": "con_private",
                            "function": "transfer_shielded",
                            "action": "transfer",
                            "output_index": 0,
                            "note_index": 0,
                            "commitment": "0xaaa",
                            "new_root": "0xroot",
                            "payload_hash": None,
                            "tag_kind": None,
                            "tag_value": None,
                            "output_payload": None,
                            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                        },
                        {
                            "output_id": 22,
                            "event_id": None,
                            "block_height": 12,
                            "tx_hash": "TX-1",
                            "tx_index": 0,
                            "contract": "con_private",
                            "function": "transfer_shielded",
                            "action": "transfer",
                            "output_index": 1,
                            "note_index": 1,
                            "commitment": "0xbbb",
                            "new_root": "0xroot",
                            "payload_hash": "0xhash",
                            "tag_kind": "sync_hint",
                            "tag_value": "0x1234",
                            "output_payload": "0x2222",
                            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                        }
                    ]
                return []

        bds = BDS(BdsConfig())
        bds.db = _ShieldedHistoryDb()

        result = await bds.get_shielded_wallet_history(
            "0x1234", limit=10, after_note_index=0
        )

        self.assertEqual(
            bds.db.fetch_calls,
            [(sql.select_shielded_wallet_history(), ["sync_hint", "0x1234", 0, 10])],
        )
        self.assertEqual(result[0]["note_index"], 0)
        self.assertEqual(result[0]["commitment"], "0xaaa")
        self.assertIsNone(result[0]["output_payload"])
        self.assertEqual(result[1]["note_index"], 1)
        self.assertEqual(result[1]["output_payload"], "0x2222")
        self.assertEqual(result[1]["payload_hash"], "0xhash")
        self.assertEqual(result[1]["function"], "transfer_shielded")
        self.assertEqual(result[1]["action"], "transfer")


if __name__ == "__main__":
    unittest.main()
