from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


def _first_non_empty(*values: object, default: object | None = None) -> object:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        return value
    return default


def _coerce_int(value: object, *, default: int) -> int:
    resolved = _first_non_empty(value, default=default)
    try:
        return int(resolved)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class BdsConfig:
    dsn: str | None = None
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "xian"
    user: str = "xian"
    password: str = ""
    pool_min_size: int = 1
    pool_max_size: int = 10
    statement_timeout_ms: int = 0
    application_name: str = "xian-bds"
    queue_max_size: int = 128
    spool_dir: str | None = None
    spool_warn_entries: int = 256
    spool_warn_bytes: int = 536_870_912
    disk_free_warn_bytes: int = 2_147_483_648

    @classmethod
    def from_runtime_settings(
        cls,
        xian_config: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> BdsConfig:
        runtime = xian_config or {}
        env = environ or os.environ
        bds_settings = runtime.get("bds", {})
        if not isinstance(bds_settings, Mapping):
            bds_settings = {}

        dsn = _first_non_empty(
            bds_settings.get("dsn"),
            env.get("XIAN_BDS_DSN"),
        )
        spool_dir = _first_non_empty(
            bds_settings.get("spool_dir"),
            env.get("XIAN_BDS_SPOOL_DIR"),
        )

        return cls(
            dsn=dsn if isinstance(dsn, str) else None,
            host=str(
                _first_non_empty(
                    bds_settings.get("host"),
                    env.get("XIAN_BDS_HOST"),
                    default="127.0.0.1",
                )
            ),
            port=_coerce_int(
                _first_non_empty(
                    bds_settings.get("port"),
                    env.get("XIAN_BDS_PORT"),
                ),
                default=5432,
            ),
            database=str(
                _first_non_empty(
                    bds_settings.get("database"),
                    env.get("XIAN_BDS_DATABASE"),
                    default="xian",
                )
            ),
            user=str(
                _first_non_empty(
                    bds_settings.get("user"),
                    env.get("XIAN_BDS_USER"),
                    default="xian",
                )
            ),
            password=str(
                _first_non_empty(
                    bds_settings.get("password"),
                    env.get("XIAN_BDS_PASSWORD"),
                    default="",
                )
            ),
            pool_min_size=_coerce_int(
                _first_non_empty(
                    bds_settings.get("pool_min_size"),
                    env.get("XIAN_BDS_POOL_MIN_SIZE"),
                ),
                default=1,
            ),
            pool_max_size=_coerce_int(
                _first_non_empty(
                    bds_settings.get("pool_max_size"),
                    env.get("XIAN_BDS_POOL_MAX_SIZE"),
                ),
                default=10,
            ),
            statement_timeout_ms=_coerce_int(
                _first_non_empty(
                    bds_settings.get("statement_timeout_ms"),
                    env.get("XIAN_BDS_STATEMENT_TIMEOUT_MS"),
                ),
                default=0,
            ),
            application_name=str(
                _first_non_empty(
                    bds_settings.get("application_name"),
                    env.get("XIAN_BDS_APPLICATION_NAME"),
                    default="xian-bds",
                )
            ),
            queue_max_size=_coerce_int(
                _first_non_empty(
                    bds_settings.get("queue_max_size"),
                    env.get("XIAN_BDS_QUEUE_MAX_SIZE"),
                ),
                default=128,
            ),
            spool_dir=str(spool_dir) if isinstance(spool_dir, str) else None,
            spool_warn_entries=_coerce_int(
                _first_non_empty(
                    bds_settings.get("spool_warn_entries"),
                    env.get("XIAN_BDS_SPOOL_WARN_ENTRIES"),
                ),
                default=256,
            ),
            spool_warn_bytes=_coerce_int(
                _first_non_empty(
                    bds_settings.get("spool_warn_bytes"),
                    env.get("XIAN_BDS_SPOOL_WARN_BYTES"),
                ),
                default=536_870_912,
            ),
            disk_free_warn_bytes=_coerce_int(
                _first_non_empty(
                    bds_settings.get("disk_free_warn_bytes"),
                    env.get("XIAN_BDS_DISK_FREE_WARN_BYTES"),
                ),
                default=2_147_483_648,
            ),
        )
