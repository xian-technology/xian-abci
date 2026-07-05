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

    async def test_prepare_schema_rejects_version_mismatch(self):
        bds = BDS(BdsConfig())
        bds.db.execute = AsyncMock()
        bds.db.fetchval = AsyncMock(return_value="6")

        with self.assertRaisesRegex(RuntimeError, "does not match runtime schema"):
            await bds._prepare_schema()

        self.assertEqual(
            bds.db.execute.await_args_list,
            [call(sql.create_meta())],
        )

    async def test_prepare_schema_rejects_unsupported_version_mismatch(self):
        bds = BDS(BdsConfig())
        bds.db.execute = AsyncMock()
        bds.db.fetchval = AsyncMock(return_value="4")

        with self.assertRaisesRegex(RuntimeError, "does not match runtime schema"):
            await bds._prepare_schema()

        self.assertEqual(
            bds.db.execute.await_args_list,
            [call(sql.create_meta())],
        )
