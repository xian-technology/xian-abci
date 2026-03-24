import json
import unittest
from datetime import UTC, datetime

from xian.services.bds import sql
from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig
from xian.services.bds.payloads import BdsTransactionPayload


class _FakeTransactionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self):
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _FakeTransactionContext()

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "OK"

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        return len(self.fetchval_calls)


class _FakeAcquireContext:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection

    def acquire(self):
        return _FakeAcquireContext(self.connection)


class BdsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_genesis_block_serializes_jsonb_columns(self):
        bds = BDS(BdsConfig())
        connection = _FakeConnection()
        bds.db.pool = _FakePool(connection)

        await bds.process_genesis_block(
            {
                "abci_genesis": {
                    "genesis": [
                        {
                            "key": "currency.balances:alice",
                            "value": {"nested": True, "amount": "10"},
                        }
                    ]
                }
            }
        )

        tx_insert = next(
            args
            for query, args in connection.execute_calls
            if query == sql.insert_transaction()
        )
        self.assertIsInstance(tx_insert[12], str)
        self.assertEqual(
            json.loads(tx_insert[12]),
            {"status": "ok", "genesis_entries": 1},
        )
        self.assertIsInstance(tx_insert[13], str)
        self.assertEqual(json.loads(tx_insert[13]), {"kind": "genesis"})
        self.assertIsInstance(tx_insert[14], str)
        self.assertEqual(json.loads(tx_insert[14])["kind"], "genesis")

        state_change = connection.fetchval_calls[0][1]
        self.assertIsInstance(state_change[7], str)
        self.assertEqual(
            json.loads(state_change[7]),
            {"nested": True, "amount": "10"},
        )

    async def test_persist_transaction_serializes_jsonb_columns(self):
        bds = BDS(BdsConfig())
        connection = _FakeConnection()

        tx = BdsTransactionPayload(
            tx_index=0,
            envelope={
                "metadata": {"signature": "deadbeef"},
                "payload": {"sender": "alice"},
            },
            payload={
                "sender": "alice",
                "nonce": 1,
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"to": "bob", "amount": "5"},
            },
            tx_result={
                "hash": "TX-1",
                "status": 0,
                "stamps_used": 12,
                "result": {"ok": True, "amount": "5"},
                "state": [
                    {
                        "key": "currency.balances:alice",
                        "value": {"balance": "95"},
                    }
                ],
                "events": [
                    {
                        "contract": "currency",
                        "event": "Transfer",
                        "signer": "alice",
                        "caller": "alice",
                        "data_indexed": {"to": "bob"},
                        "data": {"amount": "5"},
                    }
                ],
                "rewards": {},
            },
        )

        await bds._persist_transaction(
            connection,
            block_meta={"height": 1, "hash": "BLOCK-1", "nanos": 1},
            block_time=datetime(2026, 1, 1, tzinfo=UTC),
            current_index={},
            tx=tx,
        )

        tx_insert = next(
            args
            for query, args in connection.execute_calls
            if query == sql.insert_transaction()
        )
        self.assertIsInstance(tx_insert[12], str)
        self.assertEqual(
            json.loads(tx_insert[12]),
            {"ok": True, "amount": "5"},
        )
        self.assertIsInstance(tx_insert[13], str)
        self.assertEqual(json.loads(tx_insert[13])["sender"], "alice")
        self.assertIsInstance(tx_insert[14], str)
        self.assertEqual(
            json.loads(tx_insert[14])["metadata"]["signature"],
            "deadbeef",
        )

        state_change = connection.fetchval_calls[0][1]
        self.assertIsInstance(state_change[7], str)
        self.assertEqual(json.loads(state_change[7]), {"balance": "95"})

        event_insert = next(
            args
            for query, args in connection.execute_calls
            if query == sql.insert_event()
        )
        self.assertIsInstance(event_insert[8], str)
        self.assertEqual(json.loads(event_insert[8]), {"to": "bob"})
        self.assertIsInstance(event_insert[9], str)
        self.assertEqual(json.loads(event_insert[9]), {"amount": "5"})


if __name__ == "__main__":
    unittest.main()
