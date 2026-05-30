import re
from collections.abc import Sequence

import asyncpg
from loguru import logger

from xian.services.bds.config import BdsConfig


class DB:
    def __init__(self, config: BdsConfig):
        self.cfg = config
        self.pool = None

    def _acquire_timeout_seconds(self) -> float | None:
        """
        Return the pool acquire timeout in seconds (None disables).
        Surfaced as a method so the tests can monkey-patch it cleanly.
        """
        if self.cfg.acquire_timeout_ms <= 0:
            return None
        return self.cfg.acquire_timeout_ms / 1000

    def acquire(self):
        """
        Borrow a connection from the pool with the configured acquire timeout.
        Callers should use this instead of ``self.pool.acquire()`` directly so
        that the timeout is applied consistently across the service.
        """
        return self.pool.acquire(timeout=self._acquire_timeout_seconds())

    def _pool_kwargs(self) -> dict[str, object]:
        server_settings = {
            "application_name": self.cfg.application_name,
        }
        if self.cfg.statement_timeout_ms > 0:
            server_settings["statement_timeout"] = f"{self.cfg.statement_timeout_ms}ms"

        kwargs: dict[str, object] = {
            "min_size": self.cfg.pool_min_size,
            "max_size": self.cfg.pool_max_size,
            "server_settings": server_settings,
        }
        if self.cfg.dsn:
            kwargs["dsn"] = self.cfg.dsn
            return kwargs

        kwargs.update(
            {
                "user": self.cfg.user,
                "password": self.cfg.password,
                "database": self.cfg.database,
                "host": self.cfg.host,
                "port": self.cfg.port,
            }
        )
        return kwargs

    def _admin_connect_kwargs(self) -> dict[str, object]:
        return {
            "user": self.cfg.user,
            "password": self.cfg.password,
            "database": "postgres",
            "host": self.cfg.host,
            "port": self.cfg.port,
            "server_settings": {"application_name": f"{self.cfg.application_name}-bootstrap"},
        }

    @staticmethod
    def _validate_database_name(name: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid database name: {name!r}")
        return name

    async def init_pool(self):
        if not self.cfg.dsn:
            temp_conn = await asyncpg.connect(**self._admin_connect_kwargs())
            try:
                result = await temp_conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1",
                    self.cfg.database,
                )
                if not result:
                    database_name = self._validate_database_name(self.cfg.database)
                    await temp_conn.execute(f'CREATE DATABASE "{database_name}"')
            finally:
                await temp_conn.close()

        self.pool = await asyncpg.create_pool(**self._pool_kwargs())

    async def close_pool(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def execute(self, query: str, params: Sequence[object] | None = None):
        """
        This is meant for INSERT, UPDATE and DELETE statements
        that usually don't return data
        """
        bound_params = tuple(params or ())
        async with self.pool.acquire(timeout=self._acquire_timeout_seconds()) as connection:
            try:
                result = await connection.execute(query, *bound_params)
                return result
            except Exception as e:
                logger.exception(f"Error while executing SQL: {e}")
                raise e

    async def fetch(self, query: str, params: Sequence[object] | None = None):
        """
        This is meant for SELECT statements that return data
        """
        bound_params = tuple(params or ())
        async with self.pool.acquire(timeout=self._acquire_timeout_seconds()) as connection:
            try:
                result = await connection.fetch(query, *bound_params)
                return result
            except Exception as e:
                logger.exception(f"Error while executing SQL: {e}")
                raise e

    async def fetchrow(self, query: str, params: Sequence[object] | None = None):
        bound_params = tuple(params or ())
        async with self.pool.acquire(timeout=self._acquire_timeout_seconds()) as connection:
            try:
                return await connection.fetchrow(query, *bound_params)
            except Exception as e:
                logger.exception(f"Error while executing SQL: {e}")
                raise e

    async def fetchval(self, query: str, params: Sequence[object] | None = None):
        bound_params = tuple(params or ())
        async with self.pool.acquire(timeout=self._acquire_timeout_seconds()) as connection:
            try:
                return await connection.fetchval(query, *bound_params)
            except Exception as e:
                logger.exception(f"Error while executing SQL: {e}")
                raise e

    # Tables whose presence may be probed by has_entries(). Gating a bootstrap
    # decision (e.g. "run genesis persistence?") on this check means the caller
    # must never see a false negative caused by an outage, so we pin the name
    # to an explicit allowlist and let DB errors propagate.
    _HAS_ENTRIES_ALLOWED_TABLES = frozenset({"blocks", "transactions"})

    async def has_entries(self, table_name: str) -> bool:
        """
        Return True if ``table_name`` has at least one row.

        Raises if the table name is not on the allowlist, or if the query
        itself fails. The caller must distinguish "confirmed empty" from
        "could not determine" — silently returning False on any exception
        makes a DB outage indistinguishable from a fresh install, which
        would re-trigger genesis persistence on restart.
        """
        if table_name not in self._HAS_ENTRIES_ALLOWED_TABLES:
            raise ValueError(f"has_entries() called with disallowed table name: {table_name!r}")
        result = await self.fetch(f'SELECT COUNT(*) AS count FROM "{table_name}"')
        return result[0]["count"] > 0
