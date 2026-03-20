import asyncio
import json
import re
from collections.abc import Sequence

import asyncpg
from loguru import logger

from xian.services.bds.config import BdsConfig


def result_to_json(result):
    results = []
    for row in result:
        row_dict = dict(row)
        results.append(row_dict)

    # Convert the list of dictionaries to JSON
    return json.dumps(results, default=str)


class DB:
    def __init__(self, config: BdsConfig):
        self.cfg = config
        self.pool = None
        self.batch: list[tuple[str, tuple[object, ...]]] = []
        self._batch_lock = asyncio.Lock()

    def _pool_kwargs(self) -> dict[str, object]:
        server_settings = {
            "application_name": self.cfg.application_name,
        }
        if self.cfg.statement_timeout_ms > 0:
            server_settings["statement_timeout"] = (
                f"{self.cfg.statement_timeout_ms}ms"
            )

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
            "server_settings": {
                "application_name": f"{self.cfg.application_name}-bootstrap"
            },
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
                    database_name = self._validate_database_name(
                        self.cfg.database
                    )
                    await temp_conn.execute(
                        f'CREATE DATABASE "{database_name}"'
                    )
            finally:
                await temp_conn.close()

        self.pool = await asyncpg.create_pool(**self._pool_kwargs())

    async def execute(self, query: str, params: Sequence[object] | None = None):
        """
        This is meant for INSERT, UPDATE and DELETE statements
        that usually don't return data
        """
        bound_params = tuple(params or ())
        async with self.pool.acquire() as connection:
            try:
                result = await connection.execute(query, *bound_params)
                return result
            except Exception as e:
                logger.exception(f"Error while executing SQL: {e}")
                raise e

    async def add_query_to_batch(
        self, query: str, args: Sequence[object] | None = None
    ) -> None:
        async with self._batch_lock:
            self.batch.append((query, tuple(args or ())))

    async def commit_batch_to_disk(self) -> int:
        async with self._batch_lock:
            if not self.batch:
                return 0
            batch = self.batch
            self.batch = []

        async with self.pool.acquire() as connection:
            try:
                async with connection.transaction():
                    for query, params in batch:
                        await connection.execute(query, *params)
            except Exception as e:
                logger.exception(f"Error while executing SQL: {e}")
                async with self._batch_lock:
                    self.batch = list(batch) + self.batch
                raise e
        return len(batch)

    async def fetch(self, query: str, params: Sequence[object] | None = None):
        """
        This is meant for SELECT statements that return data
        """
        bound_params = tuple(params or ())
        async with self.pool.acquire() as connection:
            try:
                result = await connection.fetch(query, *bound_params)
                return result
            except Exception as e:
                logger.exception(f"Error while executing SQL: {e}")
                raise e

    async def has_entries(self, table_name: str) -> bool:
        try:
            result = await self.fetch(
                f"SELECT COUNT(*) as count FROM {table_name}"
            )
            logger.debug(result)
            return result[0]["count"] > 0
        except Exception as e:
            logger.exception(e)
            return False
