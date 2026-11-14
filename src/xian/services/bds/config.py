from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

# The [bds] table in xian.toml is the only source of BDS settings. These
# environment variables were a second, silently merged source; a node
# environment that still sets one fails at startup instead.
REMOVED_SETTING_VARIABLES = tuple(
    f"XIAN_BDS_{name}"
    for name in (
        "DSN",
        "HOST",
        "PORT",
        "DATABASE",
        "USER",
        "PASSWORD",
        "POOL_MIN_SIZE",
        "POOL_MAX_SIZE",
        "STATEMENT_TIMEOUT_MS",
        "ACQUIRE_TIMEOUT_MS",
        "APPLICATION_NAME",
        "QUEUE_MAX_SIZE",
        "CATCHUP_ENABLED",
        "CATCHUP_POLL_SECONDS",
        "RPC_URL",
        "SPOOL_DIR",
        "SPOOL_WARN_ENTRIES",
        "SPOOL_WARN_BYTES",
        "DISK_FREE_WARN_BYTES",
    )
)


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


def reject_removed_environment(environ: Mapping[str, str]) -> None:
    removed = [name for name in REMOVED_SETTING_VARIABLES if name in environ]
    if removed:
        raise ValueError(
            f"the node environment must not set {', '.join(removed)}: BDS settings "
            "come only from the [bds] table in xian.toml and the password from the "
            "file named by bds.password_file; remove these variables"
        )


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
        environ: Mapping[str, str] | None = None,
    ) -> BdsConfig:
        reject_removed_environment(os.environ if environ is None else environ)
        runtime = xian_config or {}
        settings = runtime.get("bds", {})
        if not isinstance(settings, Mapping):
            settings = {}
        if "password" in settings:
            raise ValueError(
                "xian.toml must not set bds.password: the BDS password is read from "
                "the file named by bds.password_file; remove the key"
            )
        dsn = _optional_str(settings.get("dsn"))
        validate_dsn(dsn)
        catchup_enabled = _first_non_empty(settings.get("catchup_enabled"), default=True)

        return cls(
            dsn=dsn,
            host=str(_first_non_empty(settings.get("host"), default="127.0.0.1")),
            port=_coerce_int(settings.get("port"), default=5432),
            database=str(_first_non_empty(settings.get("database"), default="xian")),
            user=str(_first_non_empty(settings.get("user"), default="xian")),
            password_file=_optional_str(settings.get("password_file")),
            pool_min_size=_coerce_int(settings.get("pool_min_size"), default=1),
            pool_max_size=_coerce_int(settings.get("pool_max_size"), default=10),
            statement_timeout_ms=_coerce_int(settings.get("statement_timeout_ms"), default=0),
            acquire_timeout_ms=_coerce_int(settings.get("acquire_timeout_ms"), default=10_000),
            application_name=str(
                _first_non_empty(settings.get("application_name"), default="xian-bds")
            ),
            queue_max_size=_coerce_int(settings.get("queue_max_size"), default=128),
            catchup_enabled=str(catchup_enabled).lower() not in {"0", "false", "no", "off"},
            catchup_poll_seconds=_coerce_float(settings.get("catchup_poll_seconds"), default=1.0),
            rpc_url=_optional_str(settings.get("rpc_url")),
            spool_dir=_optional_str(settings.get("spool_dir")),
            spool_warn_entries=_coerce_int(settings.get("spool_warn_entries"), default=256),
            spool_warn_bytes=_coerce_int(settings.get("spool_warn_bytes"), default=536_870_912),
            disk_free_warn_bytes=_coerce_int(
                settings.get("disk_free_warn_bytes"), default=2_147_483_648
            ),
        )
