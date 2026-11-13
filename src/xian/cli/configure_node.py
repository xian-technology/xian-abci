from __future__ import annotations

import json
from argparse import ArgumentParser, BooleanOptionalAction

from xian.fee_policy import (
    FEE_POLICY_FIELDS,
    SUPPORTED_TX_FEE_MODES,
)
from xian.genesis_builder import build_bundle_network_genesis
from xian.node_admin import ExistingHomeOptions, configure_existing_home
from xian.node_setup import (
    SUPPORTED_APP_LOG_LEVELS,
    SUPPORTED_BLOCK_POLICY_MODES,
    AppLoggingOptions,
    BdsOptions,
    MetricsOptions,
    NodeConfigOptions,
    SimulationOptions,
    StateSyncOptions,
)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Configure an initialized CometBFT home")
    parser.add_argument(
        "--discover-seed",
        action="append",
        help=(
            "seed node host or IP without port; queries node ID from the "
            "remote status endpoint; may be repeated"
        ),
        required=False,
    )
    parser.add_argument(
        "--seed",
        action="append",
        help=("CometBFT P2P seed in <node_id>@<host>:26656 form; may be repeated"),
        required=False,
    )
    parser.add_argument(
        "--persistent-peer",
        action="append",
        help=("CometBFT persistent peer in <node_id>@<host>:26656 form; may be repeated"),
        required=False,
    )
    parser.add_argument(
        "--moniker",
        type=str,
        help="name of your node",
        required=True,
    )
    parser.add_argument(
        "--allow-cors",
        action=BooleanOptionalAction,
        help="allow CORS on the RPC endpoint",
        required=False,
        default=True,
    )
    parser.add_argument(
        "--snapshot-url",
        type=str,
        help="URL of signed snapshot manifest JSON or snapshot archive",
        required=False,
    )
    parser.add_argument(
        "--snapshot-signing-key",
        action="append",
        help=("trusted Ed25519 public key for signed snapshot manifests; may be repeated"),
        required=False,
    )
    parser.add_argument(
        "--snapshot-expected-chain-id",
        type=str,
        help=(
            "expected chain_id for signed snapshot manifest validation; "
            "defaults to unchecked when omitted"
        ),
        required=False,
    )
    parser.add_argument(
        "--genesis-source",
        type=str,
        help=(
            "genesis source inside xian-configs, for example "
            "'networks/custom/genesis.json' or a relative path"
        ),
        required=False,
    )
    parser.add_argument(
        "--genesis-bundle",
        type=str,
        help=(
            "canonical contract bundle used to build genesis instead "
            "of copying a static genesis source"
        ),
        required=False,
    )
    parser.add_argument(
        "--chain-id",
        type=str,
        help="chain ID used when building genesis from --genesis-bundle",
        required=False,
    )
    parser.add_argument(
        "--genesis-time",
        type=str,
        help="fixed genesis time used when building genesis from --genesis-bundle",
        required=False,
    )
    parser.add_argument(
        "--runtime-feature-zk",
        action=BooleanOptionalAction,
        help="enable zk host syscalls in generated --genesis-bundle state",
        required=False,
        default=False,
    )
    parser.add_argument(
        "--validator-privkey",
        type=str,
        help="validator private key as a 64-character hex string",
        required=False,
        default=None,
    )
    parser.add_argument(
        "--prometheus",
        action=BooleanOptionalAction,
        help="enable Prometheus metrics",
        required=False,
        default=True,
    )
    parser.add_argument(
        "--bds-enabled",
        action=BooleanOptionalAction,
        help="enable the node's Blockchain Data Service runtime",
        required=False,
        default=False,
    )
    parser.add_argument(
        "--enable-pruning",
        action=BooleanOptionalAction,
        help='prune blocks according to "blocks-to-keep"',
        required=False,
        default=False,
    )
    parser.add_argument(
        "--blocks-to-keep",
        type=int,
        help='number of blocks to keep when "enable-pruning" is enabled',
        required=False,
        default=100000,
    )
    parser.add_argument(
        "--block-policy-mode",
        choices=sorted(SUPPORTED_BLOCK_POLICY_MODES),
        help="block production policy for contract time progression",
        required=False,
        default="on_demand",
    )
    parser.add_argument(
        "--block-policy-interval",
        type=str,
        help="interval used by idle_interval or periodic block policies",
        required=False,
        default="0s",
    )
    parser.add_argument(
        "--statesync-enable",
        action=BooleanOptionalAction,
        help="enable CometBFT state sync using trusted application snapshots",
        required=False,
        default=False,
    )
    parser.add_argument(
        "--statesync-rpc-server",
        action="append",
        default=[],
        help="trusted CometBFT RPC server for state sync; specify at least twice",
        required=False,
    )
    parser.add_argument(
        "--statesync-trust-height",
        type=int,
        help="trusted height for CometBFT state sync",
        required=False,
        default=0,
    )
    parser.add_argument(
        "--statesync-trust-hash",
        type=str,
        help="trusted block hash for CometBFT state sync",
        required=False,
        default="",
    )
    parser.add_argument(
        "--statesync-trust-period",
        type=str,
        help="light-client trust period for CometBFT state sync",
        required=False,
        default="168h0m0s",
    )
    parser.add_argument(
        "--metrics-enabled",
        action=BooleanOptionalAction,
        help="enable the Xian Prometheus metrics endpoint",
        required=False,
        default=True,
    )
    parser.add_argument(
        "--metrics-host",
        type=str,
        help="listen host for the Xian Prometheus metrics endpoint",
        required=False,
        default="127.0.0.1",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        help="listen port for the Xian Prometheus metrics endpoint",
        required=False,
        default=9108,
    )
    parser.add_argument(
        "--metrics-bds-refresh-seconds",
        type=float,
        help="refresh interval for BDS-derived Prometheus gauges",
        required=False,
        default=5.0,
    )
    parser.add_argument(
        "--transaction-trace-logging",
        action=BooleanOptionalAction,
        help="emit per-transaction debug summaries during block execution",
        required=False,
        default=False,
    )
    parser.add_argument(
        "--app-log-level",
        choices=sorted(SUPPORTED_APP_LOG_LEVELS),
        type=str,
        help="application log level for stderr and rotated file logs",
        required=False,
        default="INFO",
    )
    parser.add_argument(
        "--app-log-json",
        action=BooleanOptionalAction,
        help="emit application logs as structured JSON instead of plain text",
        required=False,
        default=False,
    )
    parser.add_argument(
        "--app-log-rotation-hours",
        type=int,
        help="rotate application log files after this many hours",
        required=False,
        default=1,
    )
    parser.add_argument(
        "--app-log-retention-days",
        type=int,
        help="retain rotated application logs for this many days",
        required=False,
        default=7,
    )
    parser.add_argument(
        "--app-log-max-file-mb",
        type=int,
        default=50,
        help="maximum application log segment size in MiB",
    )
    parser.add_argument(
        "--app-log-max-total-mb",
        type=int,
        default=250,
        help="maximum total application log size in MiB",
    )
    parser.add_argument(
        "--simulation-enabled",
        action=BooleanOptionalAction,
        help="enable readonly transaction simulation on this node",
        required=False,
        default=True,
    )
    parser.add_argument(
        "--simulation-max-concurrency",
        type=int,
        help="maximum concurrent simulation requests accepted by this node",
        required=False,
        default=2,
    )
    parser.add_argument(
        "--simulation-timeout-ms",
        type=int,
        help="timeout in milliseconds for a single simulation request",
        required=False,
        default=3000,
    )
    parser.add_argument(
        "--simulation-max-chi",
        type=int,
        help=(
            "operator cap on the chi one readonly simulation may spend; "
            "the chain's max_tx_chi always applies on top"
        ),
        required=False,
        default=1_000_000,
    )
    parser.add_argument(
        "--tx-fee-mode",
        choices=sorted(SUPPORTED_TX_FEE_MODES),
        help="genesis fee policy for user transactions (requires --genesis-bundle)",
        required=False,
        default=None,
    )
    parser.add_argument(
        "--max-tx-chi",
        type=int,
        help=(
            "genesis limit on chi_supplied per transaction in every fee mode "
            "(requires --genesis-bundle)"
        ),
        required=False,
        default=None,
    )
    parser.add_argument(
        "--max-block-chi",
        type=int,
        help=(
            "genesis limit on total chi_supplied per block in every fee mode "
            "(requires --genesis-bundle)"
        ),
        required=False,
        default=None,
    )
    parser.add_argument(
        "--pending-nonce-reservation-ttl-seconds",
        type=float,
        help=(
            "local mempool reservation TTL for sender nonces before stale "
            "pending transactions stop blocking retries"
        ),
        required=False,
        default=60.0,
    )
    parser.add_argument(
        "--max-pending-nonces-per-sender",
        type=int,
        help=(
            "maximum number of sequential pending nonce reservations allowed "
            "per sender in the local mempool"
        ),
        required=False,
        default=128,
    )
    parser.add_argument(
        "--bds-dsn",
        type=str,
        help="PostgreSQL DSN for the optional Blockchain Data Service",
        required=False,
        default="",
    )
    parser.add_argument(
        "--bds-host",
        type=str,
        help="PostgreSQL host for the optional Blockchain Data Service",
        required=False,
        default="",
    )
    parser.add_argument(
        "--bds-port",
        type=int,
        help="PostgreSQL port for the optional Blockchain Data Service",
        required=False,
        default=5432,
    )
    parser.add_argument(
        "--bds-database",
        type=str,
        help="PostgreSQL database name for the optional Blockchain Data Service",
        required=False,
        default="xian",
    )
    parser.add_argument(
        "--bds-user",
        type=str,
        help="PostgreSQL user for the optional Blockchain Data Service",
        required=False,
        default="",
    )
    parser.add_argument(
        "--bds-password",
        type=str,
        help="PostgreSQL password for the optional Blockchain Data Service",
        required=False,
        default="",
    )
    parser.add_argument(
        "--bds-pool-min-size",
        type=int,
        help="minimum asyncpg pool size for the optional Blockchain Data Service",
        required=False,
        default=1,
    )
    parser.add_argument(
        "--bds-pool-max-size",
        type=int,
        help="maximum asyncpg pool size for the optional Blockchain Data Service",
        required=False,
        default=10,
    )
    parser.add_argument(
        "--bds-statement-timeout-ms",
        type=int,
        help="statement timeout for BDS database sessions in milliseconds",
        required=False,
        default=0,
    )
    parser.add_argument(
        "--bds-acquire-timeout-ms",
        type=int,
        help="timeout when borrowing a BDS database connection in milliseconds",
        required=False,
        default=10_000,
    )
    parser.add_argument(
        "--bds-application-name",
        type=str,
        help="application_name reported by BDS database sessions",
        required=False,
        default="xian-bds",
    )
    parser.add_argument(
        "--bds-queue-max-size",
        type=int,
        help="maximum in-memory BDS payload queue size",
        required=False,
        default=128,
    )
    parser.add_argument(
        "--bds-catchup-enabled",
        action=BooleanOptionalAction,
        help="enable BDS catchup from CometBFT RPC",
        required=False,
        default=True,
    )
    parser.add_argument(
        "--bds-catchup-poll-seconds",
        type=float,
        help="BDS catchup poll interval in seconds",
        required=False,
        default=1.0,
    )
    parser.add_argument(
        "--bds-rpc-url",
        type=str,
        help="explicit CometBFT RPC URL for BDS catchup",
        required=False,
        default="",
    )
    parser.add_argument(
        "--bds-spool-dir",
        type=str,
        help="explicit spool directory for durable BDS block payloads",
        required=False,
        default="",
    )
    parser.add_argument(
        "--bds-spool-warn-entries",
        type=int,
        help="warning threshold for queued BDS spool entries",
        required=False,
        default=256,
    )
    parser.add_argument(
        "--bds-spool-warn-bytes",
        type=int,
        help="warning threshold for total BDS spool size in bytes",
        required=False,
        default=536_870_912,
    )
    parser.add_argument(
        "--bds-disk-free-warn-bytes",
        type=int,
        help="warning threshold for free bytes on the BDS spool filesystem",
        required=False,
        default=2_147_483_648,
    )
    return parser


def _genesis_fee_policy(args) -> dict[str, object]:
    return {
        field: getattr(args, field)
        for field in FEE_POLICY_FIELDS
        if getattr(args, field) is not None
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.genesis_bundle is not None and args.genesis_source is not None:
        raise ValueError("pass either --genesis-source or --genesis-bundle, not both")
    if args.genesis_time is not None and args.genesis_bundle is None:
        raise ValueError("--genesis-time requires --genesis-bundle")
    if args.genesis_bundle is not None and args.genesis_time is None:
        raise ValueError(
            "--genesis-bundle requires --genesis-time so all operators build one chain"
        )
    fee_policy = _genesis_fee_policy(args)
    if fee_policy and args.genesis_bundle is None:
        raise ValueError("fee policy options are genesis inputs and require --genesis-bundle")
    genesis_payload = None
    if args.genesis_bundle is not None:
        if not args.chain_id:
            raise ValueError("--chain-id is required with --genesis-bundle")
        genesis_payload = build_bundle_network_genesis(
            chain_id=args.chain_id,
            network=args.genesis_bundle,
            genesis_time=args.genesis_time,
            runtime_features={"zk": args.runtime_feature_zk},
            fee_policy=fee_policy,
        )
    result = configure_existing_home(
        ExistingHomeOptions(
            moniker=args.moniker,
            validator_private_key_hex=args.validator_privkey,
            discover_seeds=tuple(args.discover_seed or ()),
            p2p_seeds=tuple(args.seed or ()),
            p2p_persistent_peers=tuple(args.persistent_peer or ()),
            snapshot_url=args.snapshot_url,
            snapshot_signing_public_keys=tuple(args.snapshot_signing_key or ()),
            snapshot_expected_chain_id=args.snapshot_expected_chain_id,
            genesis_source=args.genesis_source,
            genesis_payload=genesis_payload,
            node_config=NodeConfigOptions(
                moniker=args.moniker,
                allow_cors=args.allow_cors,
                bds_enabled=args.bds_enabled,
                enable_pruning=args.enable_pruning,
                blocks_to_keep=args.blocks_to_keep,
                transaction_trace_logging=args.transaction_trace_logging,
                block_policy_mode=args.block_policy_mode,
                block_policy_interval=args.block_policy_interval,
                statesync=StateSyncOptions(
                    enable=args.statesync_enable,
                    rpc_servers=tuple(args.statesync_rpc_server or ()),
                    trust_height=args.statesync_trust_height,
                    trust_hash=args.statesync_trust_hash,
                    trust_period=args.statesync_trust_period,
                ),
                metrics=MetricsOptions(
                    enabled=args.metrics_enabled,
                    host=args.metrics_host,
                    port=args.metrics_port,
                    bds_refresh_seconds=args.metrics_bds_refresh_seconds,
                ),
                app_logging=AppLoggingOptions(
                    level=args.app_log_level,
                    json_logging=args.app_log_json,
                    rotation_hours=args.app_log_rotation_hours,
                    retention_days=args.app_log_retention_days,
                    max_file_mb=args.app_log_max_file_mb,
                    max_total_mb=args.app_log_max_total_mb,
                ),
                simulation=SimulationOptions(
                    enabled=args.simulation_enabled,
                    max_concurrency=args.simulation_max_concurrency,
                    timeout_ms=args.simulation_timeout_ms,
                    max_chi=args.simulation_max_chi,
                ),
                pending_nonce_reservation_ttl_seconds=(args.pending_nonce_reservation_ttl_seconds),
                max_pending_nonces_per_sender=(args.max_pending_nonces_per_sender),
                bds=BdsOptions(
                    dsn=args.bds_dsn,
                    host=args.bds_host,
                    port=args.bds_port,
                    database=args.bds_database,
                    user=args.bds_user,
                    password=args.bds_password,
                    pool_min_size=args.bds_pool_min_size,
                    pool_max_size=args.bds_pool_max_size,
                    statement_timeout_ms=args.bds_statement_timeout_ms,
                    acquire_timeout_ms=args.bds_acquire_timeout_ms,
                    application_name=args.bds_application_name,
                    queue_max_size=args.bds_queue_max_size,
                    catchup_enabled=args.bds_catchup_enabled,
                    catchup_poll_seconds=args.bds_catchup_poll_seconds,
                    rpc_url=args.bds_rpc_url,
                    spool_dir=args.bds_spool_dir,
                    spool_warn_entries=args.bds_spool_warn_entries,
                    spool_warn_bytes=args.bds_spool_warn_bytes,
                    disk_free_warn_bytes=args.bds_disk_free_warn_bytes,
                ),
                prometheus=args.prometheus,
            ),
        )
    )
    print("Make sure that port 26657 is open for the REST API")
    print("Make sure that port 26656 is open for P2P Node communication")
    print("Configuration updated")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
