from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from nacl.encoding import Base64Encoder, HexEncoder
from nacl.signing import SigningKey
from xian_runtime_types.encoding import encode

from xian import toml_utils

DEFAULT_COMETBFT_CONFIG_TOML = """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "xian-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
priv_validator_laddr = ""
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = [ "*",]
cors_allowed_methods = [ "HEAD", "GET", "POST",]
cors_allowed_headers = [ "Origin", "Accept", "Content-Type", "X-Requested-With", "X-Server-Time",]
grpc_laddr = ""
grpc_max_open_connections = 900
unsafe = false
max_open_connections = 900
max_subscription_clients = 100
max_subscriptions_per_client = 5
experimental_subscription_buffer_size = 200
experimental_websocket_write_buffer_size = 200
experimental_close_on_slow_client = false
timeout_broadcast_tx_commit = "10s"
max_body_bytes = 10485760
max_header_bytes = 1048576
tls_cert_file = ""
tls_key_file = ""
pprof_laddr = ""

[p2p]
laddr = "tcp://0.0.0.0:26656"
external_address = ""
seeds = ""
persistent_peers = ""
addr_book_file = "config/addrbook.json"
addr_book_strict = true
max_num_inbound_peers = 40
max_num_outbound_peers = 10
unconditional_peer_ids = ""
persistent_peers_max_dial_period = "0s"
flush_throttle_timeout = "100ms"
max_packet_msg_payload_size = 1024
send_rate = 5120000
recv_rate = 5120000
pex = true
seed_mode = false
private_peer_ids = ""
allow_duplicate_ip = false
handshake_timeout = "20s"
dial_timeout = "3s"

[mempool]
type = "flood"
recheck = true
broadcast = true
wal_dir = ""
size = 5000
max_txs_bytes = 1073741824
cache_size = 10000
keep-invalid-txs-in-cache = false
max_tx_bytes = 4194304
max_batch_bytes = 0
experimental_max_gossip_connections_to_persistent_peers = 0
experimental_max_gossip_connections_to_non_persistent_peers = 0

[statesync]
enable = false
rpc_servers = ""
trust_height = 0
trust_hash = ""
trust_period = "168h0m0s"
discovery_time = "15s"
temp_dir = ""
chunk_request_timeout = "10s"
chunk_fetchers = "4"

[blocksync]
version = "v0"

[consensus]
wal_file = "data/cs.wal/wal"
timeout_propose = "3s"
timeout_propose_delta = "500ms"
timeout_prevote = "1s"
timeout_prevote_delta = "500ms"
timeout_precommit = "1s"
timeout_precommit_delta = "500ms"
timeout_commit = "1s"
double_sign_check_height = 0
skip_timeout_commit = false
create_empty_blocks = false
create_empty_blocks_interval = "0s"
peer_gossip_sleep_duration = "100ms"
peer_query_maj23_sleep_duration = "2s"

[storage]
discard_abci_responses = false

[tx_index]
indexer = "kv"
psql-conn = ""

[instrumentation]
prometheus = true
prometheus_listen_addr = ":26660"
max_open_connections = 3
namespace = "cometbft"
""".strip()

DEFAULT_XIAN_CONFIG_TOML = """
bds_enabled = false
pruning_enabled = false
blocks_to_keep = 100000
metrics_enabled = true
metrics_host = "127.0.0.1"
metrics_port = 9108
metrics_bds_refresh_seconds = 5.0
transaction_trace_logging = false
app_log_level = "INFO"
app_log_json = false
app_log_rotation_hours = 1
app_log_retention_days = 7
simulation_enabled = true
simulation_max_concurrency = 2
simulation_timeout_ms = 3000
simulation_max_chi = 1000000
parallel_execution_enabled = false
parallel_execution_workers = 4
parallel_execution_min_transactions = 8
parallel_execution_max_speculative_waves = 4
parallel_execution_min_wave_acceptance_ratio = 0.25
parallel_execution_low_acceptance_min_wave_size = 8
parallel_execution_warm_workers = true
parallel_execution_access_estimates_enabled = true
pending_nonce_reservation_ttl_seconds = 60.0
max_pending_nonces_per_sender = 128

[bds]
dsn = ""
host = ""
port = 5432
database = "xian"
user = ""
password = ""
pool_min_size = 1
pool_max_size = 10
statement_timeout_ms = 0
acquire_timeout_ms = 10000
application_name = "xian-bds"
queue_max_size = 128
catchup_enabled = true
catchup_poll_seconds = 1.0
rpc_url = ""
spool_dir = ""
spool_warn_entries = 256
spool_warn_bytes = 536870912
disk_free_warn_bytes = 2147483648
""".strip()

DEFAULT_PARALLEL_EXECUTION_ENABLED = False
DEFAULT_PARALLEL_EXECUTION_WORKERS = 4
DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS = 8
DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES = 4
DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO = 0.25
DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE = 8
DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS = True
DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED = True

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
class StateSyncOptions:
    enable: bool = False
    rpc_servers: tuple[str, ...] = ()
    trust_height: int = 0
    trust_hash: str = ""
    trust_period: str = "168h0m0s"


@dataclass(frozen=True, slots=True)
class MetricsOptions:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9108
    bds_refresh_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class AppLoggingOptions:
    level: str = "INFO"
    json_logging: bool = False
    rotation_hours: int = 1
    retention_days: int = 7


@dataclass(frozen=True, slots=True)
class SimulationOptions:
    enabled: bool = True
    max_concurrency: int = 2
    timeout_ms: int = 3000
    max_chi: int = 1_000_000


@dataclass(frozen=True, slots=True)
class ParallelExecutionOptions:
    enabled: bool = DEFAULT_PARALLEL_EXECUTION_ENABLED
    workers: int = DEFAULT_PARALLEL_EXECUTION_WORKERS
    min_transactions: int = DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS
    max_speculative_waves: int = (
        DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES
    )
    min_wave_acceptance_ratio: float = (
        DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO
    )
    low_acceptance_min_wave_size: int = (
        DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE
    )
    warm_workers: bool = DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS
    access_estimates_enabled: bool = (
        DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED
    )


@dataclass(frozen=True, slots=True)
class BdsOptions:
    dsn: str = ""
    host: str = ""
    port: int = 5432
    database: str = "xian"
    user: str = ""
    password: str = ""
    pool_min_size: int = 1
    pool_max_size: int = 10
    statement_timeout_ms: int = 0
    acquire_timeout_ms: int = 10_000
    application_name: str = "xian-bds"
    queue_max_size: int = 128
    catchup_enabled: bool = True
    catchup_poll_seconds: float = 1.0
    rpc_url: str = ""
    spool_dir: str = ""
    spool_warn_entries: int = 256
    spool_warn_bytes: int = 536_870_912
    disk_free_warn_bytes: int = 2_147_483_648


@dataclass(frozen=True, slots=True)
class NodeConfigOptions:
    moniker: str
    p2p_seeds: tuple[str, ...] = ()
    p2p_persistent_peers: tuple[str, ...] = ()
    allow_cors: bool = True
    bds_enabled: bool = False
    enable_pruning: bool = False
    blocks_to_keep: int = 100000
    transaction_trace_logging: bool = False
    block_policy_mode: str = "on_demand"
    block_policy_interval: str = "0s"
    statesync: StateSyncOptions = field(default_factory=StateSyncOptions)
    metrics: MetricsOptions = field(default_factory=MetricsOptions)
    app_logging: AppLoggingOptions = field(default_factory=AppLoggingOptions)
    simulation: SimulationOptions = field(default_factory=SimulationOptions)
    parallel_execution: ParallelExecutionOptions = field(
        default_factory=ParallelExecutionOptions
    )
    pending_nonce_reservation_ttl_seconds: float = 60.0
    max_pending_nonces_per_sender: int = 128
    bds: BdsOptions = field(default_factory=BdsOptions)
    proxy_app: str = "unix:///tmp/abci.sock"
    prometheus: bool = True


def resolve_block_policy(
    *,
    mode: str = "on_demand",
    interval: str = "0s",
) -> tuple[bool, str]:
    if mode not in SUPPORTED_BLOCK_POLICY_MODES:
        raise ValueError(
            "block_policy_mode must be one of "
            f"{sorted(SUPPORTED_BLOCK_POLICY_MODES)}"
        )
    if not isinstance(interval, str) or not interval:
        raise ValueError("block_policy_interval must be a non-empty string")

    if mode == "on_demand":
        return False, "0s"

    if interval == "0s":
        raise ValueError(
            "block_policy_interval must be non-zero for idle_interval and "
            "periodic modes"
        )

    if mode == "idle_interval":
        return False, interval

    return True, interval


def resolve_simulation_settings(
    *,
    enabled: bool = True,
    max_concurrency: int = 2,
    timeout_ms: int = 3000,
    max_chi: int = 1_000_000,
) -> dict[str, Any]:
    if max_concurrency <= 0:
        raise ValueError("simulation_max_concurrency must be greater than zero")
    if timeout_ms <= 0:
        raise ValueError("simulation_timeout_ms must be greater than zero")
    if max_chi <= 0:
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
        raise ValueError(
            "metrics_bds_refresh_seconds must be greater than zero"
        )
    if (
        options.parallel_execution.enabled
        and options.parallel_execution.workers <= 0
    ):
        raise ValueError(
            "parallel_execution_workers must be greater than zero when "
            "parallel_execution_enabled is true"
        )
    if options.parallel_execution.min_transactions < 1:
        raise ValueError(
            "parallel_execution_min_transactions must be greater than zero"
        )
    if options.parallel_execution.max_speculative_waves < 0:
        raise ValueError(
            "parallel_execution_max_speculative_waves must be non-negative"
        )
    if not (
        0.0 <= options.parallel_execution.min_wave_acceptance_ratio <= 1.0
    ):
        raise ValueError(
            "parallel_execution_min_wave_acceptance_ratio must be between "
            "0.0 and 1.0"
        )
    if options.parallel_execution.low_acceptance_min_wave_size < 1:
        raise ValueError(
            "parallel_execution_low_acceptance_min_wave_size must be greater "
            "than zero"
        )
    if options.pending_nonce_reservation_ttl_seconds < 0:
        raise ValueError(
            "pending_nonce_reservation_ttl_seconds must be non-negative"
        )
    if options.max_pending_nonces_per_sender < 1:
        raise ValueError(
            "max_pending_nonces_per_sender must be greater than zero"
        )
    if options.bds.port < 1 or options.bds.port > 65535:
        raise ValueError("bds_port must be between 1 and 65535")
    if options.bds.pool_min_size < 0:
        raise ValueError("bds_pool_min_size must be non-negative")
    if options.bds.pool_max_size < 1:
        raise ValueError("bds_pool_max_size must be greater than zero")
    if options.bds.pool_min_size > options.bds.pool_max_size:
        raise ValueError(
            "bds_pool_min_size must be less than or equal to "
            "bds_pool_max_size"
        )
    if options.bds.statement_timeout_ms < 0:
        raise ValueError("bds_statement_timeout_ms must be non-negative")
    if options.bds.acquire_timeout_ms < 0:
        raise ValueError("bds_acquire_timeout_ms must be non-negative")
    if options.bds.queue_max_size < 1:
        raise ValueError("bds_queue_max_size must be greater than zero")
    if options.bds.catchup_poll_seconds <= 0:
        raise ValueError("bds_catchup_poll_seconds must be greater than zero")
    if options.bds.spool_warn_entries < 0:
        raise ValueError("bds_spool_warn_entries must be non-negative")
    if options.bds.spool_warn_bytes < 0:
        raise ValueError("bds_spool_warn_bytes must be non-negative")
    if options.bds.disk_free_warn_bytes < 0:
        raise ValueError("bds_disk_free_warn_bytes must be non-negative")


def resolve_app_logging_settings(
    *,
    level: str = "INFO",
    json_logging: bool = False,
    rotation_hours: int = 1,
    retention_days: int = 7,
) -> dict[str, Any]:
    if not isinstance(level, str):
        raise ValueError("app_log_level must be a string")
    normalized_level = level.upper()
    if normalized_level not in SUPPORTED_APP_LOG_LEVELS:
        raise ValueError(
            f"app_log_level must be one of {sorted(SUPPORTED_APP_LOG_LEVELS)}"
        )
    if rotation_hours <= 0:
        raise ValueError("app_log_rotation_hours must be greater than zero")
    if retention_days <= 0:
        raise ValueError("app_log_retention_days must be greater than zero")
    return {
        "level": normalized_level,
        "json": bool(json_logging),
        "rotation_hours": rotation_hours,
        "retention_days": retention_days,
    }


def resolve_statesync_settings(
    *,
    enable: bool = False,
    rpc_servers: Sequence[str] | None = None,
    trust_height: int = 0,
    trust_hash: str = "",
    trust_period: str = "168h0m0s",
) -> dict[str, Any]:
    if not enable:
        return {
            "enable": False,
            "rpc_servers": "",
            "trust_height": 0,
            "trust_hash": "",
            "trust_period": trust_period,
        }

    normalized_servers = [
        server.strip() for server in (rpc_servers or []) if server.strip()
    ]
    if len(normalized_servers) < 2:
        raise ValueError(
            "statesync requires at least two RPC servers for light-client "
            "verification"
        )
    if trust_height <= 0:
        raise ValueError("statesync_trust_height must be greater than zero")
    if not isinstance(trust_hash, str) or not trust_hash:
        raise ValueError(
            "statesync_trust_hash must be provided when statesync is enabled"
        )

    return {
        "enable": True,
        "rpc_servers": ",".join(normalized_servers),
        "trust_height": trust_height,
        "trust_hash": trust_hash,
        "trust_period": trust_period,
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
    signing_key, public_key_bytes, priv_key_b64 = _build_ed25519_key_material(
        seed
    )
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
        "_private_key_hex": signing_key.encode(encoder=HexEncoder).decode(
            "ascii"
        ),
    }


def generate_validator_material(
    private_key_hex: str | None = None,
) -> dict[str, Any]:
    priv_validator_key = build_priv_validator_key(
        _normalize_private_key(private_key_hex).hex()
    )
    public_key_bytes = Base64Encoder.decode(
        priv_validator_key["pub_key"]["value"].encode("ascii")
    )
    return {
        "validator_private_key_hex": priv_validator_key["_private_key_hex"],
        "validator_public_key_hex": public_key_bytes.hex(),
        "priv_validator_key": {
            key: value
            for key, value in priv_validator_key.items()
            if key != "_private_key_hex"
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
    resolved_statesync = resolve_statesync_settings(
        enable=options.statesync.enable,
        rpc_servers=options.statesync.rpc_servers,
        trust_height=options.statesync.trust_height,
        trust_hash=options.statesync.trust_hash,
        trust_period=options.statesync.trust_period,
    )
    resolved_app_logging = resolve_app_logging_settings(
        level=options.app_logging.level,
        json_logging=options.app_logging.json_logging,
        rotation_hours=options.app_logging.rotation_hours,
        retention_days=options.app_logging.retention_days,
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
    cometbft_config["consensus"]["create_empty_blocks_interval"] = (
        create_empty_blocks_interval
    )
    cometbft_config["p2p"]["seeds"] = ",".join(options.p2p_seeds)
    cometbft_config["p2p"]["persistent_peers"] = ",".join(
        options.p2p_persistent_peers
    )
    cometbft_config["rpc"]["cors_allowed_origins"] = (
        ["*"] if options.allow_cors else []
    )
    cometbft_config["instrumentation"]["prometheus"] = options.prometheus
    cometbft_config["statesync"]["enable"] = resolved_statesync["enable"]
    cometbft_config["statesync"]["rpc_servers"] = resolved_statesync[
        "rpc_servers"
    ]
    cometbft_config["statesync"]["trust_height"] = resolved_statesync[
        "trust_height"
    ]
    cometbft_config["statesync"]["trust_hash"] = resolved_statesync[
        "trust_hash"
    ]
    cometbft_config["statesync"]["trust_period"] = resolved_statesync[
        "trust_period"
    ]

    xian_config["bds_enabled"] = options.bds_enabled
    xian_config["pruning_enabled"] = options.enable_pruning
    xian_config["blocks_to_keep"] = options.blocks_to_keep
    xian_config["metrics_enabled"] = options.metrics.enabled
    xian_config["metrics_host"] = options.metrics.host
    xian_config["metrics_port"] = options.metrics.port
    xian_config["metrics_bds_refresh_seconds"] = (
        options.metrics.bds_refresh_seconds
    )
    xian_config["transaction_trace_logging"] = options.transaction_trace_logging
    xian_config["app_log_level"] = resolved_app_logging["level"]
    xian_config["app_log_json"] = resolved_app_logging["json"]
    xian_config["app_log_rotation_hours"] = resolved_app_logging[
        "rotation_hours"
    ]
    xian_config["app_log_retention_days"] = resolved_app_logging[
        "retention_days"
    ]
    xian_config["simulation_enabled"] = resolved_simulation["enabled"]
    xian_config["simulation_max_concurrency"] = resolved_simulation[
        "max_concurrency"
    ]
    xian_config["simulation_timeout_ms"] = resolved_simulation["timeout_ms"]
    xian_config["simulation_max_chi"] = resolved_simulation["max_chi"]
    xian_config["parallel_execution_enabled"] = (
        options.parallel_execution.enabled
    )
    xian_config["parallel_execution_workers"] = (
        options.parallel_execution.workers
    )
    xian_config["parallel_execution_min_transactions"] = (
        options.parallel_execution.min_transactions
    )
    xian_config["parallel_execution_max_speculative_waves"] = (
        options.parallel_execution.max_speculative_waves
    )
    xian_config["parallel_execution_min_wave_acceptance_ratio"] = (
        options.parallel_execution.min_wave_acceptance_ratio
    )
    xian_config["parallel_execution_low_acceptance_min_wave_size"] = (
        options.parallel_execution.low_acceptance_min_wave_size
    )
    xian_config["parallel_execution_warm_workers"] = (
        options.parallel_execution.warm_workers
    )
    xian_config["parallel_execution_access_estimates_enabled"] = (
        options.parallel_execution.access_estimates_enabled
    )
    xian_config["pending_nonce_reservation_ttl_seconds"] = (
        options.pending_nonce_reservation_ttl_seconds
    )
    xian_config["max_pending_nonces_per_sender"] = (
        options.max_pending_nonces_per_sender
    )
    xian_config["bds"] = {
        "dsn": options.bds.dsn,
        "host": options.bds.host,
        "port": options.bds.port,
        "database": options.bds.database,
        "user": options.bds.user,
        "password": options.bds.password,
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
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(
    path: Path, payload: dict[str, Any], *, overwrite: bool = False
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(encode(payload))
        f.write("\n")


def write_toml(
    path: Path, payload: dict[str, Any], *, overwrite: bool = False
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(toml_utils.dumps(payload))


def materialize_cometbft_home(
    *,
    home: Path,
    config: dict[str, Any],
    xian_config: dict[str, Any],
    genesis: dict[str, Any],
    priv_validator_key: dict[str, Any],
    node_key: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict[str, str]:
    config_dir = home / "config"
    data_dir = home / "data"
    storage_dir = home / "xian"

    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / "config.toml"
    xian_config_path = config_dir / "xian.toml"
    genesis_path = config_dir / "genesis.json"
    priv_validator_key_path = config_dir / "priv_validator_key.json"
    node_key_path = config_dir / "node_key.json"
    priv_validator_state_path = data_dir / "priv_validator_state.json"

    write_toml(config_path, config, overwrite=overwrite)
    write_toml(xian_config_path, xian_config, overwrite=overwrite)
    write_json(genesis_path, genesis, overwrite=overwrite)

    validator_payload = {
        key: value
        for key, value in priv_validator_key.items()
        if key != "_private_key_hex"
    }
    write_json(
        priv_validator_key_path,
        validator_payload,
        overwrite=overwrite,
    )

    if not node_key_path.exists():
        node_key_payload = node_key or build_node_key()
        write_json(
            node_key_path,
            {"priv_key": node_key_payload["priv_key"]},
            overwrite=False,
        )

    if not priv_validator_state_path.exists():
        write_json(
            priv_validator_state_path,
            build_priv_validator_state(),
            overwrite=False,
        )

    return {
        "home": str(home),
        "config_path": str(config_path),
        "xian_config_path": str(xian_config_path),
        "genesis_path": str(genesis_path),
        "priv_validator_key_path": str(priv_validator_key_path),
        "node_key_path": str(node_key_path),
        "priv_validator_state_path": str(priv_validator_state_path),
        "storage_path": str(storage_dir),
    }
