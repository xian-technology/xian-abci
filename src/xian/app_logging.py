from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from xian.constants import Constants
from xian.node_setup import resolve_app_logging_settings
from xian.utils.cometbft import load_tendermint_config
from xian.utils.tx import tx_hash_from_tx

SUPPORTED_APP_LOG_LEVELS = (
    "TRACE",
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
)

_PLAIN_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
    "{name}:{function}:{line} | {message}{extra[context]}"
)
_CONTEXT_ORDER = (
    "stage",
    "block_height",
    "block_hash",
    "tx_index",
    "tx_hash",
    "raw_tx_hash",
    "sender",
    "contract",
    "function",
    "nonce",
    "status",
)


def default_logs_directory(constants: Constants = Constants()) -> Path:
    return constants.STORAGE_HOME / "logs"


def _format_log_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def format_log_context(fields: dict[str, Any]) -> str:
    items: list[str] = []
    seen: set[str] = set()
    for key in _CONTEXT_ORDER:
        value = fields.get(key)
        if value is None:
            continue
        items.append(f"{key}={_format_log_value(value)}")
        seen.add(key)

    for key in sorted(fields):
        if key in seen or key == "context":
            continue
        value = fields[key]
        if value is None:
            continue
        items.append(f"{key}={_format_log_value(value)}")

    if not items:
        return ""
    return " | " + " ".join(items)


def build_log_fields(
    *,
    stage: str | None = None,
    tx: dict | None = None,
    payload: dict | None = None,
    raw_tx: bytes | None = None,
    tx_hash: str | None = None,
    block_height: int | None = None,
    block_hash: str | None = None,
    tx_index: int | None = None,
    status: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if stage is not None:
        fields["stage"] = stage
    if block_height is not None:
        fields["block_height"] = block_height
    if block_hash is not None:
        fields["block_hash"] = block_hash
    if tx_index is not None:
        fields["tx_index"] = tx_index
    if status is not None:
        fields["status"] = status

    if raw_tx is not None:
        fields["raw_tx_hash"] = hashlib.sha256(raw_tx).hexdigest()
        fields["raw_tx_bytes"] = len(raw_tx)

    resolved_payload = payload
    if resolved_payload is None and isinstance(tx, dict):
        candidate = tx.get("payload")
        if isinstance(candidate, dict):
            resolved_payload = candidate

    if tx_hash is None and isinstance(tx, dict):
        try:
            tx_hash = tx_hash_from_tx(tx)
        except Exception:
            tx_hash = None
    if tx_hash is not None:
        fields["tx_hash"] = tx_hash

    if isinstance(resolved_payload, dict):
        for source_key, target_key in (
            ("sender", "sender"),
            ("contract", "contract"),
            ("function", "function"),
            ("nonce", "nonce"),
        ):
            value = resolved_payload.get(source_key)
            if value is not None:
                fields[target_key] = value

    if extra:
        for key, value in extra.items():
            if value is not None:
                fields[key] = value

    fields["context"] = format_log_context(fields)
    return fields


def configure_logging(
    constants: Constants = Constants(),
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if config is None:
        try:
            config = load_tendermint_config(constants)
        except FileNotFoundError:
            config = {}

    xian_config = config.get("xian", {}) if isinstance(config, dict) else {}
    settings = resolve_app_logging_settings(
        level=xian_config.get("app_log_level", "INFO"),
        json_logging=xian_config.get("app_log_json", False),
        rotation_hours=xian_config.get("app_log_rotation_hours", 1),
        retention_days=xian_config.get("app_log_retention_days", 7),
    )
    logs_dir = default_logs_directory(constants)
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.configure(extra={"context": ""})

    sink_kwargs: dict[str, Any] = {
        "level": settings["level"],
        "serialize": settings["json"],
    }
    if not settings["json"]:
        sink_kwargs["format"] = _PLAIN_LOG_FORMAT

    logger.add(sys.stderr, **sink_kwargs)
    logger.add(
        logs_dir / "xian-abci-{time:YYYY-MM-DD_HH-mm-ss}.log",
        rotation=timedelta(hours=settings["rotation_hours"]),
        retention=timedelta(days=settings["retention_days"]),
        compression="zip",
        enqueue=True,
        **sink_kwargs,
    )
    logger.bind(
        **build_log_fields(
            stage="startup",
            extra={
                "logs_dir": str(logs_dir),
                "app_log_level": settings["level"],
                "app_log_json": settings["json"],
                "app_log_rotation_hours": settings["rotation_hours"],
                "app_log_retention_days": settings["retention_days"],
            },
        )
    ).info("Configured Xian application logging")
    return {
        **settings,
        "logs_dir": str(logs_dir),
    }
