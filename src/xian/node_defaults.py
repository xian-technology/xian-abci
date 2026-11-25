"""Application defaults shared by runtime, node setup and backend CLI configuration."""

import tomllib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NodeRuntimeDefaults:
    bds_enabled: bool = False
    pruning_enabled: bool = False
    blocks_to_keep: int = 100000
    metrics_enabled: bool = True
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9108
    metrics_bds_refresh_seconds: float = 5.0
    transaction_trace_logging: bool = False
    app_log_level: str = "INFO"
    app_log_json: bool = False
    app_log_rotation_hours: int = 1
    app_log_retention_days: int = 7
    app_log_max_file_mb: int = 50
    app_log_max_total_mb: int = 250
    simulation_enabled: bool = True
    simulation_max_concurrency: int = 2
    simulation_timeout_ms: int = 3000
    pending_nonce_reservation_ttl_seconds: float = 60.0
    max_pending_nonces_per_sender: int = 128
    # None leaves the chain fee policy in charge of the simulation budget.
    simulation_max_chi: int | None = None


RUNTIME_DEFAULTS = NodeRuntimeDefaults()

# These addresses and limits also seed the CometBFT template.
DEFAULT_ABCI_PROXY_APP = "unix:///tmp/abci.sock"
DEFAULT_RPC_LADDR = "tcp://127.0.0.1:26657"
DEFAULT_MAX_TX_BYTES = 4 * 1024 * 1024


DEFAULT_BLOCK_POLICY_MODE = "on_demand"
DEFAULT_BLOCK_POLICY_INTERVAL = "0s"


DEFAULT_COMETBFT_CONFIG_TOML = f"""
version = "0.40.0"
proxy_app = "{DEFAULT_ABCI_PROXY_APP}"
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
laddr = "{DEFAULT_RPC_LADDR}"
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

[p2p.libp2p]
enabled = false
bootstrap_peers = []

[mempool]
type = "flood"
recheck = true
broadcast = true
wal_dir = ""
size = 5000
max_txs_bytes = 1073741824
cache_size = 10000
keep-invalid-txs-in-cache = false
max_tx_bytes = {DEFAULT_MAX_TX_BYTES}
max_batch_bytes = 0
experimental_max_gossip_connections_to_persistent_peers = 0
experimental_max_gossip_connections_to_non_persistent_peers = 0

[statesync]
enable = false

[blocksync]
version = "v0"
adaptive_sync = false

[consensus]
wal_file = "data/cs.wal/wal"
timeout_propose = "2s"
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

COMETBFT_DEFAULTS = tomllib.loads(DEFAULT_COMETBFT_CONFIG_TOML)
