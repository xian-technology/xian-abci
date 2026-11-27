"""Validate the supported node-local configuration fields."""

from dataclasses import fields

from xian.node_defaults import NodeRuntimeDefaults
from xian.services.bds.config import BdsConfig
from xian.services.snapshot_limits import SnapshotLimits


def validate_node_config(config: dict[str, object] | None) -> BdsConfig:
    allowed = {field.name for field in fields(NodeRuntimeDefaults)}
    allowed.update("snapshot_" + field.name for field in fields(SnapshotLimits))
    # Homes prepared by older releases may still have these unused settings.
    allowed.update(
        {"bds", "pending_nonce_reservation_ttl_seconds", "max_pending_nonces_per_sender"}
    )
    unknown = sorted(set(config or {}) - allowed)
    if unknown:
        raise ValueError(f"unknown xian.toml key(s): {', '.join(unknown)}")
    return BdsConfig.from_runtime_settings(config)
