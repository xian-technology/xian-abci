"""Application defaults shared by runtime, node setup and backend CLI configuration."""

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
