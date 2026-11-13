from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from xian.constants import Constants
from xian.log_retention import BoundedLogSink
from xian.log_safety import context_text, sanitize_record
from xian.node_setup import resolve_app_logging_settings
from xian.utils.cometbft import load_xian_config
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
_APP_LOG_LEVEL_ORDER = {level: index for index, level in enumerate(SUPPORTED_APP_LOG_LEVELS)}

_PLAIN_LOG_FORMAT = (
    "{time:YYYY-MM-DDTHH:mm:ss.SSS}Z | {level:<8} | "
    "xian-abci/{name}:{function}:{line} | {message}{extra[context]}"
)


def default_logs_directory(constants: Constants = Constants()) -> Path:
    return constants.STORAGE_HOME / "logs"


def format_log_context(fields: dict[str, Any]) -> str:
    return context_text(fields)


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


def log_level_includes(configured_level: str, target_level: str) -> bool:
    normalized_configured = configured_level.upper()
    normalized_target = target_level.upper()
    configured_rank = _APP_LOG_LEVEL_ORDER.get(normalized_configured)
    target_rank = _APP_LOG_LEVEL_ORDER.get(normalized_target)
    if configured_rank is None or target_rank is None:
        raise ValueError(f"log levels must be one of {list(_APP_LOG_LEVEL_ORDER.keys())}")
    return configured_rank <= target_rank


def configure_logging(
    constants: Constants = Constants(),
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if config is None:
        try:
            xian_config = load_xian_config(constants)
        except FileNotFoundError:
            xian_config = {}
    else:
        xian_config = config.get("xian", config) if isinstance(config, dict) else {}
    settings = resolve_app_logging_settings(
        level=xian_config.get("app_log_level", "INFO"),
        json_logging=xian_config.get("app_log_json", False),
        rotation_hours=xian_config.get("app_log_rotation_hours", 1),
        retention_days=xian_config.get("app_log_retention_days", 7),
        max_file_mb=xian_config.get("app_log_max_file_mb", 50),
        max_total_mb=xian_config.get("app_log_max_total_mb", 250),
    )
    logs_dir = default_logs_directory(constants)
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.configure(extra={"context": ""}, patcher=sanitize_record)

    sink_kwargs: dict[str, Any] = {
        "level": settings["level"],
        "serialize": settings["json"],
        "diagnose": False,
        "backtrace": False,
        "format": _PLAIN_LOG_FORMAT,
    }

    logger.add(sys.stderr, enqueue=True, **sink_kwargs)
    logger.add(
        BoundedLogSink(
            logs_dir,
            max_file_bytes=settings["max_file_mb"] * 1024**2,
            max_total_bytes=settings["max_total_mb"] * 1024**2,
            rotation_seconds=settings["rotation_hours"] * 3600,
            retention_seconds=settings["retention_days"] * 86400,
        ),
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
                "app_log_max_file_mb": settings["max_file_mb"],
                "app_log_max_total_mb": settings["max_total_mb"],
            },
        )
    ).info("Configured Xian application logging")
    return {
        **settings,
        "logs_dir": str(logs_dir),
    }
