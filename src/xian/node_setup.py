from __future__ import annotations

import errno
import hashlib
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from nacl.encoding import Base64Encoder, HexEncoder
from nacl.signing import SigningKey
from xian_runtime_types.encoding import encode_wire, loads_wire_json

from xian import toml_utils
from xian.execution_policy import VM_EXECUTION_MODE
from xian.node_defaults import (
    COMETBFT_DEFAULTS as _COMETBFT_DEFAULTS,
)
from xian.node_defaults import (
    DEFAULT_ABCI_PROXY_APP,
    DEFAULT_BLOCK_POLICY_INTERVAL,
    DEFAULT_BLOCK_POLICY_MODE,
    RUNTIME_DEFAULTS,
)
from xian.node_defaults import (
    DEFAULT_COMETBFT_CONFIG_TOML as DEFAULT_COMETBFT_CONFIG_TOML,
)
from xian.services.bds.config import BdsConfig, validate_dsn
from xian.state_sync_policy import validate_comet_state_sync_disabled
from xian.utils.cometbft import split_host_port

DEFAULT_XIAN_CONFIG_TOML = toml_utils.dumps(
    {name: value for name, value in asdict(RUNTIME_DEFAULTS).items() if value is not None}
).strip()

SUPPORTED_BLOCK_POLICY_MODES = {"on_demand", "idle_interval", "periodic"}
SUPPORTED_APP_LOG_LEVELS = {
    "TRACE",
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
}


@dataclass(frozen=True, slots=True)
class MetricsOptions:
    enabled: bool = RUNTIME_DEFAULTS.metrics_enabled
    host: str = RUNTIME_DEFAULTS.metrics_host
    port: int = RUNTIME_DEFAULTS.metrics_port
    bds_refresh_seconds: float = RUNTIME_DEFAULTS.metrics_bds_refresh_seconds


@dataclass(frozen=True, slots=True)
class AppLoggingOptions:
    level: str = RUNTIME_DEFAULTS.app_log_level
    json_logging: bool = RUNTIME_DEFAULTS.app_log_json
    rotation_hours: int = RUNTIME_DEFAULTS.app_log_rotation_hours
    retention_days: int = RUNTIME_DEFAULTS.app_log_retention_days
    max_file_mb: int = RUNTIME_DEFAULTS.app_log_max_file_mb
    max_total_mb: int = RUNTIME_DEFAULTS.app_log_max_total_mb


@dataclass(frozen=True, slots=True)
class SimulationOptions:
    enabled: bool = RUNTIME_DEFAULTS.simulation_enabled
    max_concurrency: int = RUNTIME_DEFAULTS.simulation_max_concurrency
    timeout_ms: int = RUNTIME_DEFAULTS.simulation_timeout_ms
    # Operator cap on one readonly simulation. None leaves the chain's
    # max_tx_chi in charge; a value only ever lowers it.
    max_chi: int | None = RUNTIME_DEFAULTS.simulation_max_chi


# Where the containerized node reads the BDS password: the Docker secret that
# xian-stack and xian-deploy mount from one owner-only host file, which the
# Postgres service also reads (POSTGRES_PASSWORD_FILE).
BDS_PASSWORD_SECRET_PATH = "/run/secrets/xian_bds_password"


# Shared values come from the runtime config. BdsOptions keeps the container
# overrides and uses empty strings for optional TOML settings.
_BDS_RUNTIME_DEFAULTS = BdsConfig()


@dataclass(frozen=True, slots=True)
class BdsOptions:
    """[bds] in xian.toml; the defaults name the Postgres service of the
    xian-stack and xian-deploy compose projects."""

    dsn: str = ""
    host: str = "postgres"
    port: int = _BDS_RUNTIME_DEFAULTS.port
    database: str = _BDS_RUNTIME_DEFAULTS.database
    user: str = _BDS_RUNTIME_DEFAULTS.user
    password_file: str = BDS_PASSWORD_SECRET_PATH
    pool_min_size: int = _BDS_RUNTIME_DEFAULTS.pool_min_size
    pool_max_size: int = _BDS_RUNTIME_DEFAULTS.pool_max_size
    statement_timeout_ms: int = _BDS_RUNTIME_DEFAULTS.statement_timeout_ms
    acquire_timeout_ms: int = _BDS_RUNTIME_DEFAULTS.acquire_timeout_ms
    application_name: str = _BDS_RUNTIME_DEFAULTS.application_name
    queue_max_size: int = _BDS_RUNTIME_DEFAULTS.queue_max_size
    catchup_enabled: bool = _BDS_RUNTIME_DEFAULTS.catchup_enabled
    catchup_poll_seconds: float = _BDS_RUNTIME_DEFAULTS.catchup_poll_seconds
    # Catch-up reads blocks from the node's own CometBFT RPC (integrated image).
    rpc_url: str = "http://127.0.0.1:26657"
    spool_dir: str = ""
    spool_warn_entries: int = _BDS_RUNTIME_DEFAULTS.spool_warn_entries
    spool_warn_bytes: int = _BDS_RUNTIME_DEFAULTS.spool_warn_bytes
    disk_free_warn_bytes: int = _BDS_RUNTIME_DEFAULTS.disk_free_warn_bytes


@dataclass(frozen=True, slots=True)
class NodeConfigOptions:
    moniker: str
    p2p_seeds: tuple[str, ...] = ()
    p2p_persistent_peers: tuple[str, ...] = ()
    # host:port other nodes dial; without it CometBFT advertises its listen
    # address (0.0.0.0), which no peer's address book accepts.
    p2p_external_address: str = _COMETBFT_DEFAULTS["p2p"]["external_address"]
    allow_cors: bool = bool(_COMETBFT_DEFAULTS["rpc"]["cors_allowed_origins"])
    bds_enabled: bool = RUNTIME_DEFAULTS.bds_enabled
    enable_pruning: bool = RUNTIME_DEFAULTS.pruning_enabled
    blocks_to_keep: int = RUNTIME_DEFAULTS.blocks_to_keep
    transaction_trace_logging: bool = RUNTIME_DEFAULTS.transaction_trace_logging
    block_policy_mode: str = DEFAULT_BLOCK_POLICY_MODE
    block_policy_interval: str = DEFAULT_BLOCK_POLICY_INTERVAL
    metrics: MetricsOptions = field(default_factory=MetricsOptions)
    app_logging: AppLoggingOptions = field(default_factory=AppLoggingOptions)
    simulation: SimulationOptions = field(default_factory=SimulationOptions)
    bds: BdsOptions = field(default_factory=BdsOptions)
    proxy_app: str = DEFAULT_ABCI_PROXY_APP
    prometheus: bool = _COMETBFT_DEFAULTS["instrumentation"]["prometheus"]


_NODE_DEFAULTS = NodeConfigOptions(moniker="")


def resolve_block_policy(
    *,
    mode: str = _NODE_DEFAULTS.block_policy_mode,
    interval: str = _NODE_DEFAULTS.block_policy_interval,
) -> tuple[bool, str]:
    if mode not in SUPPORTED_BLOCK_POLICY_MODES:
        raise ValueError(f"block_policy_mode must be one of {sorted(SUPPORTED_BLOCK_POLICY_MODES)}")
    if not isinstance(interval, str) or not interval:
        raise ValueError("block_policy_interval must be a non-empty string")

    if mode == "on_demand":
        return False, "0s"

    if interval == "0s":
        raise ValueError(
            "block_policy_interval must be non-zero for idle_interval and periodic modes"
        )

    if mode == "idle_interval":
        return False, interval

    return True, interval


def resolve_p2p_external_address(value: str) -> str:
    """Return ``value`` when peers can dial it as ``host:port``; "" stays unset."""
    if value == "":
        return value
    host, port = split_host_port(value)
    if not host or "/" in host or _is_unspecified_ip(host):
        raise ValueError(f"p2p external_address needs a dialable host: {value!r}")
    if port is None or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"p2p external_address needs a port 1-65535: {value!r}")
    return value


def _is_unspecified_ip(host: str) -> bool:
    try:
        return ip_address(host).is_unspecified
    except ValueError:
        return False


def resolve_simulation_settings(
    *,
    enabled: bool = _NODE_DEFAULTS.simulation.enabled,
    max_concurrency: int = _NODE_DEFAULTS.simulation.max_concurrency,
    timeout_ms: int = _NODE_DEFAULTS.simulation.timeout_ms,
    max_chi: int | None = _NODE_DEFAULTS.simulation.max_chi,
) -> dict[str, Any]:
    if max_concurrency <= 0:
        raise ValueError("simulation_max_concurrency must be greater than zero")
    if timeout_ms <= 0:
        raise ValueError("simulation_timeout_ms must be greater than zero")
    if max_chi is not None and max_chi <= 0:
        raise ValueError("simulation_max_chi must be greater than zero")
    return {
        "enabled": enabled,
        "max_concurrency": max_concurrency,
        "timeout_ms": timeout_ms,
        "max_chi": max_chi,
    }


def validate_node_runtime_settings(options: NodeConfigOptions) -> None:
    if options.blocks_to_keep <= 0:
        raise ValueError("blocks_to_keep must be greater than zero")
    if options.metrics.port < 1 or options.metrics.port > 65535:
        raise ValueError("metrics_port must be between 1 and 65535")
    if options.metrics.bds_refresh_seconds <= 0:
        raise ValueError("metrics_bds_refresh_seconds must be greater than zero")
    validate_bds_options(options.bds)


# Integer [bds] settings and their lowest valid value.
_BDS_LOWER_BOUNDS = (
    ("pool_min_size", 0),
    ("pool_max_size", 1),
    ("statement_timeout_ms", 0),
    ("acquire_timeout_ms", 0),
    ("queue_max_size", 1),
    ("spool_warn_entries", 0),
    ("spool_warn_bytes", 0),
    ("disk_free_warn_bytes", 0),
)


def validate_bds_options(bds: BdsOptions) -> None:
    if bds.port < 1 or bds.port > 65535:
        raise ValueError("bds_port must be between 1 and 65535")
    validate_dsn(bds.dsn)
    for name, minimum in _BDS_LOWER_BOUNDS:
        if getattr(bds, name) < minimum:
            bound = "non-negative" if minimum == 0 else "greater than zero"
            raise ValueError(f"bds_{name} must be {bound}")
    if bds.pool_min_size > bds.pool_max_size:
        raise ValueError("bds_pool_min_size must be less than or equal to bds_pool_max_size")
    if bds.catchup_poll_seconds <= 0:
        raise ValueError("bds_catchup_poll_seconds must be greater than zero")


def resolve_app_logging_settings(
    *,
    level: str = _NODE_DEFAULTS.app_logging.level,
    json_logging: bool = _NODE_DEFAULTS.app_logging.json_logging,
    rotation_hours: int = _NODE_DEFAULTS.app_logging.rotation_hours,
    retention_days: int = _NODE_DEFAULTS.app_logging.retention_days,
    max_file_mb: int = _NODE_DEFAULTS.app_logging.max_file_mb,
    max_total_mb: int = _NODE_DEFAULTS.app_logging.max_total_mb,
) -> dict[str, Any]:
    if not isinstance(level, str):
        raise ValueError("app_log_level must be a string")
    normalized_level = level.upper()
    if normalized_level not in SUPPORTED_APP_LOG_LEVELS:
        raise ValueError(f"app_log_level must be one of {sorted(SUPPORTED_APP_LOG_LEVELS)}")
    limits = {
        "rotation_hours": rotation_hours,
        "retention_days": retention_days,
        "max_file_mb": max_file_mb,
        "max_total_mb": max_total_mb,
    }
    for name, value in limits.items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"app_log_{name} must be a positive integer")
    if max_total_mb < max_file_mb:
        raise ValueError("app_log_max_total_mb must cover app_log_max_file_mb")
    return {
        "level": normalized_level,
        "json": bool(json_logging),
        "rotation_hours": rotation_hours,
        "retention_days": retention_days,
        "max_file_mb": max_file_mb,
        "max_total_mb": max_total_mb,
    }


def _normalize_private_key(private_key_hex: str | None) -> bytes:
    if private_key_hex is None:
        return secrets.token_bytes(32)

    if len(private_key_hex) != 64:
        raise ValueError("private key must be a 64-character hex string")

    try:
        return bytes.fromhex(private_key_hex)
    except ValueError as exc:
        raise ValueError("private key must be valid hex") from exc


def _build_ed25519_key_material(seed: bytes) -> tuple[SigningKey, bytes, str]:
    signing_key = SigningKey(seed=seed)
    verify_key = signing_key.verify_key
    priv_key_with_pub = signing_key.encode() + verify_key.encode()
    priv_key_b64 = Base64Encoder.encode(priv_key_with_pub).decode("ascii")
    return signing_key, verify_key.encode(), priv_key_b64


def build_priv_validator_key(private_key_hex: str) -> dict[str, Any]:
    seed = _normalize_private_key(private_key_hex)
    signing_key, public_key_bytes, priv_key_b64 = _build_ed25519_key_material(seed)
    address_bytes = hashlib.sha256(public_key_bytes).digest()[:20]

    return {
        "address": address_bytes.hex().upper(),
        "pub_key": {
            "type": "tendermint/PubKeyEd25519",
            "value": Base64Encoder.encode(public_key_bytes).decode("ascii"),
        },
        "priv_key": {
            "type": "tendermint/PrivKeyEd25519",
            "value": priv_key_b64,
        },
        "_private_key_hex": signing_key.encode(encoder=HexEncoder).decode("ascii"),
    }


def generate_validator_material(
    private_key_hex: str | None = None,
) -> dict[str, Any]:
    priv_validator_key = build_priv_validator_key(_normalize_private_key(private_key_hex).hex())
    public_key_bytes = Base64Encoder.decode(priv_validator_key["pub_key"]["value"].encode("ascii"))
    return {
        "validator_private_key_hex": priv_validator_key["_private_key_hex"],
        "validator_public_key_hex": public_key_bytes.hex(),
        "priv_validator_key": {
            key: value for key, value in priv_validator_key.items() if key != "_private_key_hex"
        },
    }


def build_node_key(private_key_hex: str | None = None) -> dict[str, Any]:
    seed = _normalize_private_key(private_key_hex)
    _, public_key_bytes, priv_key_b64 = _build_ed25519_key_material(seed)
    node_id = hashlib.sha256(public_key_bytes).digest()[:20].hex().upper()

    return {
        "node_id": node_id,
        "priv_key": {
            "type": "tendermint/PrivKeyEd25519",
            "value": priv_key_b64,
        },
    }


def build_priv_validator_state() -> dict[str, Any]:
    return {
        "height": "0",
        "round": 0,
        "step": 0,
    }


def render_node_configs(
    *,
    options: NodeConfigOptions,
) -> dict[str, dict[str, Any]]:
    validate_node_runtime_settings(options)
    cometbft_config = toml_utils.loads(DEFAULT_COMETBFT_CONFIG_TOML)
    xian_config = toml_utils.loads(DEFAULT_XIAN_CONFIG_TOML)
    create_empty_blocks, create_empty_blocks_interval = resolve_block_policy(
        mode=options.block_policy_mode,
        interval=options.block_policy_interval,
    )
    resolved_app_logging = resolve_app_logging_settings(
        level=options.app_logging.level,
        json_logging=options.app_logging.json_logging,
        rotation_hours=options.app_logging.rotation_hours,
        retention_days=options.app_logging.retention_days,
        max_file_mb=options.app_logging.max_file_mb,
        max_total_mb=options.app_logging.max_total_mb,
    )
    resolved_simulation = resolve_simulation_settings(
        enabled=options.simulation.enabled,
        max_concurrency=options.simulation.max_concurrency,
        timeout_ms=options.simulation.timeout_ms,
        max_chi=options.simulation.max_chi,
    )
    cometbft_config["proxy_app"] = options.proxy_app
    cometbft_config["moniker"] = options.moniker
    cometbft_config["consensus"]["create_empty_blocks"] = create_empty_blocks
    cometbft_config["consensus"]["create_empty_blocks_interval"] = create_empty_blocks_interval
    cometbft_config["p2p"]["seeds"] = ",".join(options.p2p_seeds)
    cometbft_config["p2p"]["persistent_peers"] = ",".join(options.p2p_persistent_peers)
    cometbft_config["p2p"]["external_address"] = resolve_p2p_external_address(
        options.p2p_external_address
    )
    cometbft_config["rpc"]["cors_allowed_origins"] = ["*"] if options.allow_cors else []
    cometbft_config["instrumentation"]["prometheus"] = options.prometheus

    xian_config["bds_enabled"] = options.bds_enabled
    xian_config["pruning_enabled"] = options.enable_pruning
    xian_config["blocks_to_keep"] = options.blocks_to_keep
    xian_config["metrics_enabled"] = options.metrics.enabled
    xian_config["metrics_host"] = options.metrics.host
    xian_config["metrics_port"] = options.metrics.port
    xian_config["metrics_bds_refresh_seconds"] = options.metrics.bds_refresh_seconds
    xian_config["transaction_trace_logging"] = options.transaction_trace_logging
    xian_config["app_log_level"] = resolved_app_logging["level"]
    xian_config["app_log_json"] = resolved_app_logging["json"]
    xian_config["app_log_rotation_hours"] = resolved_app_logging["rotation_hours"]
    xian_config["app_log_retention_days"] = resolved_app_logging["retention_days"]
    xian_config["app_log_max_file_mb"] = resolved_app_logging["max_file_mb"]
    xian_config["app_log_max_total_mb"] = resolved_app_logging["max_total_mb"]
    xian_config["simulation_enabled"] = resolved_simulation["enabled"]
    xian_config["simulation_max_concurrency"] = resolved_simulation["max_concurrency"]
    xian_config["simulation_timeout_ms"] = resolved_simulation["timeout_ms"]
    if resolved_simulation["max_chi"] is not None:
        xian_config["simulation_max_chi"] = resolved_simulation["max_chi"]
    xian_config["bds"] = {
        "dsn": options.bds.dsn,
        "host": options.bds.host,
        "port": options.bds.port,
        "database": options.bds.database,
        "user": options.bds.user,
        "password_file": options.bds.password_file,
        "pool_min_size": options.bds.pool_min_size,
        "pool_max_size": options.bds.pool_max_size,
        "statement_timeout_ms": options.bds.statement_timeout_ms,
        "acquire_timeout_ms": options.bds.acquire_timeout_ms,
        "application_name": options.bds.application_name,
        "queue_max_size": options.bds.queue_max_size,
        "catchup_enabled": options.bds.catchup_enabled,
        "catchup_poll_seconds": options.bds.catchup_poll_seconds,
        "rpc_url": options.bds.rpc_url,
        "spool_dir": options.bds.spool_dir,
        "spool_warn_entries": options.bds.spool_warn_entries,
        "spool_warn_bytes": options.bds.spool_warn_bytes,
        "disk_free_warn_bytes": options.bds.disk_free_warn_bytes,
    }
    return {"cometbft": cometbft_config, "xian": xian_config}


def render_cometbft_config(*, options: NodeConfigOptions) -> dict[str, Any]:
    return render_node_configs(options=options)["cometbft"]


def render_xian_config(*, options: NodeConfigOptions) -> dict[str, Any]:
    return render_node_configs(options=options)["xian"]


def load_genesis(path: Path) -> dict[str, Any]:
    return loads_wire_json(path.read_bytes())


# CometBFT keeps key material owner-only.
SECRET_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def _write_text(path: Path, text: str, *, overwrite: bool, private: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists")
    if not private:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return

    _write_private_text(path, text, overwrite=overwrite)


def _write_private_text(path: Path, text: str, *, overwrite: bool) -> None:
    if path.is_symlink():
        raise OSError(errno.ELOOP, "Refusing a symbolic link for a private file", str(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as target:
            temporary = Path(target.name)
            os.fchmod(target.fileno(), SECRET_FILE_MODE)
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _json_file_text(payload: dict[str, Any]) -> str:
    return encode_wire(payload, vm_profile=VM_EXECUTION_MODE) + "\n"


def write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    overwrite: bool = False,
    private: bool = False,
) -> None:
    _write_text(path, _json_file_text(payload), overwrite=overwrite, private=private)


class GenesisIntegrityError(ValueError):
    """The genesis is not the one the network pinned with ``genesis_sha256``."""


def genesis_sha256(genesis: dict[str, Any]) -> str:
    """A network's genesis pin: the SHA-256 of the ``config/genesis.json`` bytes a home gets.

    Any formatting of the same document hashes alike, because a home always
    receives the canonical encoding; any other document hashes differently.
    """
    return _text_sha256(_json_file_text(genesis))


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_genesis_sha256(observed: str, expected: str | None) -> None:
    """Refuse a genesis whose SHA-256 differs from the network's pin (no pin: accept)."""
    if expected is not None and observed != expected:
        raise GenesisIntegrityError(
            f"genesis sha256 {observed} does not match the network's pinned "
            f"genesis_sha256 {expected}; refusing this genesis"
        )


def write_toml(
    path: Path,
    payload: dict[str, Any],
    *,
    overwrite: bool = False,
    private: bool = False,
) -> None:
    _write_text(path, toml_utils.dumps(payload), overwrite=overwrite, private=private)


def _signing_state_path(home: Path, config: dict[str, Any]) -> Path:
    return home / config.get("priv_validator_state_file", "data/priv_validator_state.json")


def _has_runtime_data(home: Path) -> bool:
    return any(path.is_dir() and any(path.iterdir()) for path in (home / "data", home / "xian"))


def _require_existing_signing_state(home: Path, config: dict[str, Any]) -> None:
    previous_path = home / "config/config.toml"
    previous = toml_utils.load(previous_path) if previous_path.exists() else {}
    previous_key = home / previous.get("priv_validator_key_file", "config/priv_validator_key.json")
    default_key = home / "config/priv_validator_key.json"
    if not (previous_key.exists() or default_key.exists() or _has_runtime_data(home)):
        return
    original = _signing_state_path(home, previous)
    if original.resolve() != _signing_state_path(home, config).resolve():
        raise ValueError("Refusing to change an existing validator's signing state path.")
    if not original.is_file():
        raise ValueError(
            "Existing node has no validator signing state; restore its original signing state "
            "instead of recreating a zero-height file."
        )


def materialize_cometbft_home(
    *,
    home: Path,
    config: dict[str, Any],
    xian_config: dict[str, Any],
    genesis: dict[str, Any],
    priv_validator_key: dict[str, Any],
    node_key: dict[str, Any] | None = None,
    overwrite: bool = False,
    expected_genesis_sha256: str | None = None,
) -> dict[str, str]:
    validate_comet_state_sync_disabled(config)
    genesis_text = _json_file_text(genesis)
    installed_genesis_sha256 = _text_sha256(genesis_text)
    verify_genesis_sha256(installed_genesis_sha256, expected_genesis_sha256)
    _require_existing_signing_state(home, config)
    config_dir = home / "config"
    data_dir = home / "data"
    storage_dir = home / "xian"

    config_dir.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    data_dir.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    storage_dir.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / "config.toml"
    xian_config_path = config_dir / "xian.toml"
    genesis_path = config_dir / "genesis.json"
    priv_validator_key_path = config_dir / "priv_validator_key.json"
    node_key_path = config_dir / "node_key.json"
    priv_validator_state_path = _signing_state_path(home, config)

    write_toml(config_path, config, overwrite=overwrite)
    write_toml(xian_config_path, xian_config, overwrite=overwrite, private=True)
    _write_text(genesis_path, genesis_text, overwrite=overwrite, private=False)

    validator_payload = {
        key: value for key, value in priv_validator_key.items() if key != "_private_key_hex"
    }
    write_json(
        priv_validator_key_path,
        validator_payload,
        overwrite=overwrite,
        private=True,
    )

    if not node_key_path.exists():
        node_key_payload = node_key or build_node_key()
        write_json(
            node_key_path,
            {"priv_key": node_key_payload["priv_key"]},
            overwrite=False,
            private=True,
        )

    if not priv_validator_state_path.exists():
        write_json(
            priv_validator_state_path,
            build_priv_validator_state(),
            overwrite=False,
            private=True,
        )

    return {
        "home": str(home),
        "config_path": str(config_path),
        "xian_config_path": str(xian_config_path),
        "genesis_path": str(genesis_path),
        "genesis_sha256": installed_genesis_sha256,
        "priv_validator_key_path": str(priv_validator_key_path),
        "node_key_path": str(node_key_path),
        "priv_validator_state_path": str(priv_validator_state_path),
        "storage_path": str(storage_dir),
    }
