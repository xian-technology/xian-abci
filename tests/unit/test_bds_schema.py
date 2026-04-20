import unittest
from unittest.mock import AsyncMock, call

from xian.services.bds import sql
from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig


class BdsSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_schema_rejects_existing_tables_without_metadata(self):
        bds = BDS(BdsConfig())
        bds.db.execute = AsyncMock()
        bds.db.fetchval = AsyncMock(side_effect=[None, True])

        with self.assertRaisesRegex(RuntimeError, "metadata is missing"):
            await bds._prepare_schema()

        self.assertEqual(
            bds.db.execute.await_args_list,
            [call(sql.create_meta())],
        )

    async def test_prepare_schema_resets_version_mismatch(self):
        bds = BDS(BdsConfig())
        bds.db.execute = AsyncMock()
        bds.db.fetchval = AsyncMock(return_value="4")

        await bds._prepare_schema()

        executed_queries = [
            awaited.args[0] for awaited in bds.db.execute.await_args_list
        ]
        self.assertEqual(executed_queries[0], sql.create_meta())
        self.assertIn(sql.drop_all_tables(), executed_queries)
        self.assertEqual(executed_queries[2], sql.create_meta())
        self.assertEqual(executed_queries[-1], sql.upsert_schema_version())
