import unittest

from xian.services.bds.config import BdsConfig
from xian.services.bds.database import DB


class _FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transaction_entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.connection.transaction_exited += 1
        return False


class _FakeConnection:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.executed = []
        self.transaction_entered = 0
        self.transaction_exited = 0

    def transaction(self):
        return _FakeTransaction(self)

    async def execute(self, query, *params):
        self.executed.append((query, params))
        if self.fail:
            raise RuntimeError("boom")
        return "OK"


class _AcquireContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AcquireContext(self.connection)


class BdsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_batch_uses_transaction_and_clears_batch(self):
        db = DB(BdsConfig())
        connection = _FakeConnection()
        db.pool = _FakePool(connection)

        await db.add_query_to_batch("SELECT 1", [1])
        await db.add_query_to_batch("SELECT 2", [2])

        count = await db.commit_batch_to_disk()

        self.assertEqual(count, 2)
        self.assertEqual(connection.transaction_entered, 1)
        self.assertEqual(connection.transaction_exited, 1)
        self.assertEqual(len(connection.executed), 2)
        self.assertEqual(db.batch, [])

    async def test_commit_batch_requeues_on_failure(self):
        db = DB(BdsConfig())
        connection = _FakeConnection(fail=True)
        db.pool = _FakePool(connection)

        await db.add_query_to_batch("SELECT 1", [1])

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await db.commit_batch_to_disk()

        self.assertEqual(len(db.batch), 1)
        self.assertEqual(db.batch[0][0], "SELECT 1")


if __name__ == "__main__":
    unittest.main()
