from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any, Sequence

from contracting.execution.tracer import SUPPORTED_TRACER_MODES
from nacl.encoding import Base64Encoder, HexEncoder
from nacl.signing import SigningKey
from xian_runtime_types.encoding import encode

from xian import toml_utils

DEFAULT_CONFIG_TOML = """
version = "0.38.7"
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
max_body_bytes = 1000000
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
max_tx_bytes = 1048576
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

[xian]
block_service_mode = false
pruning_enabled = false
blocks_to_keep = 100000
tracer_mode = "python_line_v1"
parallel_execution_enabled = false
parallel_execution_workers = 0
parallel_execution_min_transactions = 8

[xian.bds]
dsn = ""
host = ""
port = 5432
database = "xian"
user = ""
password = ""
pool_min_size = 1
pool_max_size = 10
statement_timeout_ms = 0
application_name = "xian-bds"
""".strip()

SUPPORTED_BLOCK_POLICY_MODES = {"on_demand", "idle_interval", "periodic"}


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


def resolve_tracer_mode(mode: str = "python_line_v1") -> str:
    if mode not in SUPPORTED_TRACER_MODES:
        raise ValueError(
            f"tracer_mode must be one of {sorted(SUPPORTED_TRACER_MODES)}"
        )
    return mode


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


def render_cometbft_config(
    *,
    moniker: str,
    seed_nodes: Sequence[str] | None = None,
    allow_cors: bool = True,
    service_node: bool = False,
    enable_pruning: bool = False,
    blocks_to_keep: int = 100000,
    block_policy_mode: str = "on_demand",
    block_policy_interval: str = "0s",
    tracer_mode: str = "python_line_v1",
    parallel_execution_enabled: bool = False,
    parallel_execution_workers: int = 0,
    parallel_execution_min_transactions: int = 8,
    bds_dsn: str = "",
    bds_host: str = "",
    bds_port: int = 5432,
    bds_database: str = "xian",
    bds_user: str = "",
    bds_password: str = "",
    bds_pool_min_size: int = 1,
    bds_pool_max_size: int = 10,
    bds_statement_timeout_ms: int = 0,
    bds_application_name: str = "xian-bds",
    proxy_app: str = "unix:///tmp/abci.sock",
    prometheus: bool = True,
) -> dict[str, Any]:
    config = toml_utils.loads(DEFAULT_CONFIG_TOML)
    create_empty_blocks, create_empty_blocks_interval = resolve_block_policy(
        mode=block_policy_mode,
        interval=block_policy_interval,
    )
    resolved_tracer_mode = resolve_tracer_mode(tracer_mode)
    config["proxy_app"] = proxy_app
    config["moniker"] = moniker
    config["consensus"]["create_empty_blocks"] = create_empty_blocks
    config["consensus"]["create_empty_blocks_interval"] = (
        create_empty_blocks_interval
    )
    config["p2p"]["seeds"] = ",".join(seed_nodes or [])
    config["rpc"]["cors_allowed_origins"] = ["*"] if allow_cors else []
    config["instrumentation"]["prometheus"] = prometheus
    config["xian"] = {
        "block_service_mode": service_node,
        "pruning_enabled": enable_pruning,
        "blocks_to_keep": blocks_to_keep,
        "tracer_mode": resolved_tracer_mode,
        "parallel_execution_enabled": parallel_execution_enabled,
        "parallel_execution_workers": parallel_execution_workers,
        "parallel_execution_min_transactions": (
            parallel_execution_min_transactions
        ),
        "bds": {
            "dsn": bds_dsn,
            "host": bds_host,
            "port": bds_port,
            "database": bds_database,
            "user": bds_user,
            "password": bds_password,
            "pool_min_size": bds_pool_min_size,
            "pool_max_size": bds_pool_max_size,
            "statement_timeout_ms": bds_statement_timeout_ms,
            "application_name": bds_application_name,
        },
    }
    return config


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
    genesis_path = config_dir / "genesis.json"
    priv_validator_key_path = config_dir / "priv_validator_key.json"
    node_key_path = config_dir / "node_key.json"
    priv_validator_state_path = data_dir / "priv_validator_state.json"

    write_toml(config_path, config, overwrite=overwrite)
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
        "genesis_path": str(genesis_path),
        "priv_validator_key_path": str(priv_validator_key_path),
        "node_key_path": str(node_key_path),
        "priv_validator_state_path": str(priv_validator_state_path),
        "storage_path": str(storage_dir),
    }
