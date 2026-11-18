from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


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
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_float(value: object, *, default: float) -> float:
    resolved = _first_non_empty(value, default=default)
    try:
        normalized = float(resolved)
        return normalized if math.isfinite(normalized) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def validate_dsn(dsn: str | None) -> None:
    """The password lives only in bds.password_file, never in the DSN."""
    if not dsn:
        return
    parts = urlsplit(dsn)
    if parts.password is not None or "password" in parse_qs(parts.query):
        raise ValueError(
            "bds.dsn must not contain a password: put it in the file named by bds.password_file"
        )


@dataclass(frozen=True)
class BdsConfig:
    dsn: str | None = None
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "xian"
    user: str = "xian"
    # A file holding the database password (read when connecting). None means
    # no password.
    password_file: str | None = None
    pool_min_size: int = 1
    pool_max_size: int = 10
    statement_timeout_ms: int = 0
    # How long to wait when borrowing a connection from the pool. Zero means
    # wait forever (asyncpg's default); positive values cap the wait so an
    # exhausted pool fails fast instead of hanging the ABCI app.
    acquire_timeout_ms: int = 10_000
    application_name: str = "xian-bds"
    queue_max_size: int = 128
    catchup_enabled: bool = True
    catchup_poll_seconds: float = 1.0
    rpc_url: str | None = None
    spool_dir: str | None = None
    spool_warn_entries: int = 256
    spool_warn_bytes: int = 536_870_912
    disk_free_warn_bytes: int = 2_147_483_648

    def read_password(self) -> str:
        """The password from ``password_file``; trailing newlines are not part
        of it (as for Postgres' POSTGRES_PASSWORD_FILE)."""
        if self.password_file is None:
            return ""
        try:
            with open(self.password_file, encoding="utf-8") as handle:
                return handle.read().rstrip("\n")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"bds.password_file {self.password_file} cannot be read: {exc}"
            ) from exc

    @classmethod
    def from_runtime_settings(
        cls,
        xian_config: Mapping[str, Any] | None = None,
    ) -> BdsConfig:
        runtime = xian_config or {}
        settings = runtime.get("bds", {})
        if not isinstance(settings, Mapping):
            settings = {}
        unknown = sorted(set(settings) - {field.name for field in fields(cls)})
        if unknown:
            raise ValueError(f"unknown bds key(s): {', '.join(unknown)}")
        dsn = _optional_str(settings.get("dsn"))
        validate_dsn(dsn)
        defaults = cls()
        catchup_enabled = _first_non_empty(
            settings.get("catchup_enabled"), default=defaults.catchup_enabled
        )

        return cls(
            dsn=dsn,
            host=str(_first_non_empty(settings.get("host"), default=defaults.host)),
            port=_coerce_int(settings.get("port"), default=defaults.port),
            database=str(_first_non_empty(settings.get("database"), default=defaults.database)),
            user=str(_first_non_empty(settings.get("user"), default=defaults.user)),
            password_file=_optional_str(settings.get("password_file")),
            pool_min_size=_coerce_int(
                settings.get("pool_min_size"), default=defaults.pool_min_size
            ),
            pool_max_size=_coerce_int(
                settings.get("pool_max_size"), default=defaults.pool_max_size
            ),
            statement_timeout_ms=_coerce_int(
                settings.get("statement_timeout_ms"), default=defaults.statement_timeout_ms
            ),
            acquire_timeout_ms=_coerce_int(
                settings.get("acquire_timeout_ms"), default=defaults.acquire_timeout_ms
            ),
            application_name=str(
                _first_non_empty(
                    settings.get("application_name"), default=defaults.application_name
                )
            ),
            queue_max_size=_coerce_int(
                settings.get("queue_max_size"), default=defaults.queue_max_size
            ),
            catchup_enabled=str(catchup_enabled).lower() not in {"0", "false", "no", "off"},
            catchup_poll_seconds=_coerce_float(
                settings.get("catchup_poll_seconds"), default=defaults.catchup_poll_seconds
            ),
            rpc_url=_optional_str(settings.get("rpc_url")),
            spool_dir=_optional_str(settings.get("spool_dir")),
            spool_warn_entries=_coerce_int(
                settings.get("spool_warn_entries"), default=defaults.spool_warn_entries
            ),
            spool_warn_bytes=_coerce_int(
                settings.get("spool_warn_bytes"), default=defaults.spool_warn_bytes
            ),
            disk_free_warn_bytes=_coerce_int(
                settings.get("disk_free_warn_bytes"), default=defaults.disk_free_warn_bytes
            ),
        )
