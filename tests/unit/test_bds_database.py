import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from xian.services.bds.config import BdsConfig
from xian.services.bds.database import DB


class _FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.acquire_timeouts = []

    def acquire(self, timeout=None):
        self.acquire_timeouts.append(timeout)
        connection = self.connection

        class _Ctx:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


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

    def test_acquire_timeout_disabled_when_nonpositive(self):
        db = DB(BdsConfig(acquire_timeout_ms=0))

        self.assertIsNone(db._acquire_timeout_seconds())

    def test_pool_kwargs_with_dsn_skips_discrete_params(self):
        db = DB(BdsConfig(dsn="postgresql://user:pw@db:5432/xian"))

        kwargs = db._pool_kwargs()

        self.assertEqual(kwargs["dsn"], "postgresql://user:pw@db:5432/xian")
        self.assertNotIn("host", kwargs)
        self.assertNotIn("user", kwargs)
        self.assertEqual(
            kwargs["server_settings"], {"application_name": "xian-bds"}
        )

    def test_pool_kwargs_includes_statement_timeout_when_configured(self):
        db = DB(BdsConfig(statement_timeout_ms=750))

        kwargs = db._pool_kwargs()

        self.assertEqual(
            kwargs["server_settings"]["statement_timeout"], "750ms"
        )
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["database"], "xian")

    def test_admin_connect_kwargs_targets_postgres_database(self):
        db = DB(BdsConfig(user="admin", password="secret"))

        kwargs = db._admin_connect_kwargs()

        self.assertEqual(kwargs["database"], "postgres")
        self.assertEqual(
            kwargs["server_settings"]["application_name"],
            "xian-bds-bootstrap",
        )

    def test_validate_database_name_rejects_injection(self):
        self.assertEqual(DB._validate_database_name("xian_2"), "xian_2")

        for bad_name in ("bad-name", 'bad"; DROP DATABASE x', "1leading", ""):
            with self.assertRaisesRegex(ValueError, "invalid database name"):
                DB._validate_database_name(bad_name)

    async def test_init_pool_with_dsn_skips_bootstrap(self):
        db = DB(BdsConfig(dsn="postgresql://user:pw@db:5432/xian"))
        pool = object()

        with (
            patch(
                "xian.services.bds.database.asyncpg.connect",
                new_callable=AsyncMock,
            ) as connect,
            patch(
                "xian.services.bds.database.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ) as create_pool,
        ):
            await db.init_pool()

        connect.assert_not_awaited()
        create_pool.assert_awaited_once()
        self.assertIs(db.pool, pool)

    async def test_init_pool_creates_missing_database(self):
        db = DB(BdsConfig())
        temp_conn = AsyncMock()
        temp_conn.fetchval.return_value = None

        with (
            patch(
                "xian.services.bds.database.asyncpg.connect",
                new_callable=AsyncMock,
                return_value=temp_conn,
            ),
            patch(
                "xian.services.bds.database.asyncpg.create_pool",
                new_callable=AsyncMock,
            ),
        ):
            await db.init_pool()

        temp_conn.execute.assert_awaited_once_with('CREATE DATABASE "xian"')
        temp_conn.close.assert_awaited_once()

    async def test_init_pool_skips_create_when_database_exists(self):
        db = DB(BdsConfig())
        temp_conn = AsyncMock()
        temp_conn.fetchval.return_value = 1

        with (
            patch(
                "xian.services.bds.database.asyncpg.connect",
                new_callable=AsyncMock,
                return_value=temp_conn,
            ),
            patch(
                "xian.services.bds.database.asyncpg.create_pool",
                new_callable=AsyncMock,
            ),
        ):
            await db.init_pool()

        temp_conn.execute.assert_not_awaited()
        temp_conn.close.assert_awaited_once()

    async def test_close_pool_is_idempotent(self):
        db = DB(BdsConfig())
        pool = AsyncMock()
        db.pool = pool

        await db.close_pool()
        await db.close_pool()

        pool.close.assert_awaited_once()
        self.assertIsNone(db.pool)

    async def test_execute_binds_params_and_applies_timeout(self):
        db = DB(BdsConfig())
        connection = AsyncMock()
        connection.execute.return_value = "INSERT 0 1"
        db.pool = _FakePool(connection)

        result = await db.execute("INSERT INTO t VALUES ($1, $2)", ["a", 2])

        self.assertEqual(result, "INSERT 0 1")
        connection.execute.assert_awaited_once_with(
            "INSERT INTO t VALUES ($1, $2)", "a", 2
        )
        self.assertEqual(db.pool.acquire_timeouts, [10.0])

    async def test_fetch_propagates_query_errors(self):
        db = DB(BdsConfig())
        connection = AsyncMock()
        connection.fetch.side_effect = RuntimeError("boom")
        db.pool = _FakePool(connection)

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await db.fetch("SELECT 1")

    async def test_fetchrow_and_fetchval_bind_params(self):
        db = DB(BdsConfig())
        connection = AsyncMock()
        connection.fetchrow.return_value = {"id": 1}
        connection.fetchval.return_value = 42
        db.pool = _FakePool(connection)

        row = await db.fetchrow("SELECT * FROM t WHERE id = $1", [1])
        value = await db.fetchval("SELECT COUNT(*) FROM t")

        self.assertEqual(row, {"id": 1})
        self.assertEqual(value, 42)
        connection.fetchrow.assert_awaited_once_with(
            "SELECT * FROM t WHERE id = $1", 1
        )
        connection.fetchval.assert_awaited_once_with("SELECT COUNT(*) FROM t")
