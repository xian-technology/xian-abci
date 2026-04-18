import unittest
from unittest.mock import AsyncMock, MagicMock

from xian.services.bds.config import BdsConfig
from xian.services.bds.database import DB


class BdsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_has_entries_queries_allowlisted_table(self):
        db = DB(BdsConfig())
        db.fetch = AsyncMock(return_value=[{"count": 1}])

        result = await db.has_entries("blocks")

        self.assertTrue(result)
        db.fetch.assert_awaited_once_with(
            'SELECT COUNT(*) AS count FROM "blocks"'
        )

    async def test_has_entries_rejects_disallowed_table(self):
        db = DB(BdsConfig())
        db.fetch = AsyncMock()

        with self.assertRaisesRegex(ValueError, "disallowed table name"):
            await db.has_entries("blocks; drop table blocks")

        db.fetch.assert_not_awaited()

    async def test_has_entries_propagates_database_errors(self):
        db = DB(BdsConfig())
        db.fetch = AsyncMock(side_effect=RuntimeError("db unavailable"))

        with self.assertRaisesRegex(RuntimeError, "db unavailable"):
            await db.has_entries("transactions")

    def test_acquire_uses_configured_timeout(self):
        db = DB(BdsConfig(acquire_timeout_ms=2500))
        db.pool = MagicMock()
        db.pool.acquire.return_value = object()

        db.acquire()

        db.pool.acquire.assert_called_once_with(timeout=2.5)
