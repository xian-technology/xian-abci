from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from xian_accounts import is_valid_ed25519_key, verify_message

from xian.config_paths import resolve_genesis_source
from xian.constants import Constants as c
from xian.node_setup import (
    DEFAULT_PARALLEL_EXECUTION_ENABLED,
    DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS,
    DEFAULT_PARALLEL_EXECUTION_WORKERS,
    AppLoggingOptions,
    BdsOptions,
    ExecutionOptions,
    MetricsOptions,
    NodeConfigOptions,
    ParallelExecutionOptions,
    SimulationOptions,
    StateSyncOptions,
    build_priv_validator_key,
    render_node_configs,
    write_json,
    write_toml,
)
from xian.toml_utils import load as load_toml

_ROOT_CONFIG_KEYS_TO_PRESERVE = (
    "db_backend",
    "db_dir",
    "genesis_file",
    "priv_validator_key_file",
    "priv_validator_state_file",
    "node_key_file",
    "abci",
    "log_level",
    "log_format",
    "version",
)
_NESTED_CONFIG_KEYS_TO_PRESERVE = (
    ("rpc", "laddr"),
    ("p2p", "laddr"),
)


@dataclass(frozen=True, slots=True)
class ExistingHomeOptions:
    moniker: str
    validator_private_key_hex: str | None = None
    home: Path = c.COMETBFT_HOME
    seed_node: str | None = None
    seed_node_address: str | None = None
    snapshot_url: str | None = None
    snapshot_signing_public_keys: tuple[str, ...] = ()
    snapshot_expected_chain_id: str | None = None
    copy_genesis: bool = False
    genesis_source: str | None = None
    genesis_payload: dict[str, Any] | None = None
    node_config: NodeConfigOptions | None = None


def load_existing_cometbft_config(
    config_path: Path = c.COMETBFT_CONFIG,
) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError("initialize CometBFT first")

    with open(config_path, "r", encoding="utf-8") as handle:
        return load_toml(handle)


def preserve_runtime_config(
    rendered_config: dict[str, Any], existing_config: dict[str, Any]
) -> dict[str, Any]:
    for key in _ROOT_CONFIG_KEYS_TO_PRESERVE:
        rendered_config[key] = existing_config[key]

    for section, key in _NESTED_CONFIG_KEYS_TO_PRESERVE:
        rendered_config[section][key] = existing_config[section][key]

    return rendered_config


def fetch_seed_node_status(
    seed_node: str,
    *,
    attempts: int = 10,
    timeout_seconds: float = 3.0,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        try:
            return _fetch_json_url(
                f"http://{seed_node}:26657/status",
                timeout=timeout_seconds,
            )
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ):
            if attempt == attempts - 1:
                break
            sleep(poll_interval_seconds)

    return None


def resolve_seed_nodes(
    *,
    seed_node: str | None = None,
    seed_node_address: str | None = None,
) -> list[str]:
    if seed_node is not None:
        status = fetch_seed_node_status(seed_node)
        if status is None:
            raise RuntimeError(
                f"failed to get node information from seed node {seed_node}"
            )

        node_id = status["result"]["node_info"]["id"]
        return [f"{node_id}@{seed_node}:26656"]

    if seed_node_address is not None:
        return [f"{seed_node_address}:26656"]

    return []


def _safe_extract_tar_archive(archive_path: Path, target_path: Path) -> None:
    target_root = target_path.resolve()
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            member_path = (target_root / member.name).resolve()
            if not member_path.is_relative_to(target_root):
                raise ValueError(
                    f"snapshot archive contains invalid path: {member.name}"
                )
        archive.extractall(path=target_root, filter="data")


def _download_snapshot_archive(snapshot_url: str, target_path: Path) -> Path:
    parsed_url = urlparse(snapshot_url)
    filename = Path(parsed_url.path).name or "snapshot.tar.gz"
    archive_path = target_path / filename
    archive_path.write_bytes(_download_binary_url(snapshot_url, timeout=30))
    return archive_path


def _fetch_json_url(url: str, *, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset)
    return json.loads(payload)


def _download_binary_url(url: str, *, timeout: float) -> bytes:
    with urlopen(url, timeout=timeout) as response:
        return response.read()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_snapshot_manifest_public_keys(
    public_keys: list[str] | None,
) -> list[str]:
    if public_keys is None:
        return []

    normalized: list[str] = []
    for value in public_keys:
        if not isinstance(value, str):
            raise ValueError(
                "trusted snapshot signing keys must be hex strings"
            )
        key = value.strip()
        if not is_valid_ed25519_key(key):
            raise ValueError(f"invalid snapshot signing key: {value!r}")
        normalized.append(key)
    return normalized


def _canonical_snapshot_manifest_payload(manifest: dict[str, Any]) -> str:
    payload = {
        "manifest_version": manifest["manifest_version"],
        "chain_id": manifest["chain_id"],
        "height": manifest["height"],
        "snapshot_url": manifest["snapshot_url"],
        "snapshot_sha256": manifest["snapshot_sha256"],
        "signing_public_key": manifest["signing_public_key"],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _verify_snapshot_manifest(
    manifest: dict[str, Any],
    *,
    trusted_manifest_public_keys: list[str],
    expected_chain_id: str | None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("snapshot manifest must be a JSON object")

    manifest_version = manifest.get("manifest_version")
    if isinstance(manifest_version, bool) or not isinstance(
        manifest_version, int
    ):
        raise ValueError("snapshot manifest_version must be an integer")
    if manifest_version != 1:
        raise ValueError(
            f"unsupported snapshot manifest_version: {manifest_version}"
        )

    chain_id = manifest.get("chain_id")
    if not isinstance(chain_id, str) or chain_id == "":
        raise ValueError("snapshot manifest chain_id must be a string")
    if expected_chain_id is not None and chain_id != expected_chain_id:
        raise ValueError(
            "snapshot manifest chain_id mismatch: "
            f"expected {expected_chain_id}, got {chain_id}"
        )

    height = manifest.get("height")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise ValueError("snapshot manifest height must be a positive integer")

    snapshot_url = manifest.get("snapshot_url")
    if not isinstance(snapshot_url, str) or snapshot_url == "":
        raise ValueError("snapshot manifest snapshot_url must be a string")

    snapshot_sha256 = manifest.get("snapshot_sha256")
    if not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64:
        raise ValueError(
            "snapshot manifest snapshot_sha256 must be a 64-character hex string"
        )
    try:
        bytes.fromhex(snapshot_sha256)
    except ValueError as ex:
        raise ValueError(
            "snapshot manifest snapshot_sha256 must be a 64-character hex string"
        ) from ex
    snapshot_sha256 = snapshot_sha256.lower()

    signing_public_key = manifest.get("signing_public_key")
    if not isinstance(signing_public_key, str) or not is_valid_ed25519_key(
        signing_public_key
    ):
        raise ValueError(
            "snapshot manifest signing_public_key must be a valid Ed25519 hex key"
        )
    if signing_public_key not in trusted_manifest_public_keys:
        raise ValueError("snapshot manifest signer is not trusted")

    signature = manifest.get("signature")
    if not isinstance(signature, str) or signature == "":
        raise ValueError("snapshot manifest signature must be a hex string")

    normalized_manifest = {
        "manifest_version": manifest_version,
        "chain_id": chain_id,
        "height": height,
        "snapshot_url": snapshot_url,
        "snapshot_sha256": snapshot_sha256,
        "signing_public_key": signing_public_key,
        "signature": signature,
    }
    payload = _canonical_snapshot_manifest_payload(normalized_manifest)
    if not verify_message(signing_public_key, payload, signature):
        raise ValueError("snapshot manifest signature verification failed")
    return normalized_manifest


def apply_snapshot_archive(
    snapshot_url: str,
    home: Path,
    *,
    expected_sha256: str | None = None,
    trusted_manifest_public_keys: list[str] | None = None,
    expected_chain_id: str | None = None,
) -> str:
    home.mkdir(parents=True, exist_ok=True)
    trusted_keys = _normalize_snapshot_manifest_public_keys(
        trusted_manifest_public_keys
    )
    effective_snapshot_url = snapshot_url
    effective_sha256 = expected_sha256
    if expected_sha256 is None:
        if not trusted_keys:
            raise ValueError(
                "remote snapshot restore requires expected_sha256 or "
                "trusted snapshot signing keys"
            )
        manifest = _fetch_json_url(snapshot_url, timeout=30)
        verified_manifest = _verify_snapshot_manifest(
            manifest,
            trusted_manifest_public_keys=trusted_keys,
            expected_chain_id=expected_chain_id,
        )
        effective_snapshot_url = verified_manifest["snapshot_url"]
        effective_sha256 = verified_manifest["snapshot_sha256"]

    with tempfile.TemporaryDirectory(dir=home) as tmp_dir:
        archive_path = _download_snapshot_archive(
            effective_snapshot_url,
            Path(tmp_dir),
        )
        if not tarfile.is_tarfile(archive_path):
            raise ValueError("snapshot archive must be a .tar or .tar.gz file")
        if effective_sha256 is not None:
            actual_sha256 = _sha256_file(archive_path)
            if actual_sha256.lower() != effective_sha256.lower():
                raise ValueError(
                    "snapshot archive sha256 mismatch: "
                    f"expected {effective_sha256}, got {actual_sha256}"
                )

        for directory in (home / "data", home / "xian"):
            if directory.exists():
                shutil.rmtree(directory)

        _safe_extract_tar_archive(archive_path, home)
        return archive_path.name


def resolve_home_relative_path(home: Path, configured_path: str) -> Path:
    path = Path(configured_path)
    if path.is_absolute():
        return path
    return home / path


def configure_existing_home(
    *,
    options: ExistingHomeOptions | None = None,
    moniker: str | None = None,
    validator_private_key_hex: str | None = None,
    home: Path = c.COMETBFT_HOME,
    allow_cors: bool = True,
    seed_node: str | None = None,
    seed_node_address: str | None = None,
    snapshot_url: str | None = None,
    snapshot_signing_public_keys: list[str] | None = None,
    snapshot_expected_chain_id: str | None = None,
    copy_genesis: bool = False,
    genesis_source: str | None = None,
    genesis_payload: dict[str, Any] | None = None,
    prometheus: bool = True,
    service_node: bool = False,
    enable_pruning: bool = False,
    blocks_to_keep: int = 100000,
    block_policy_mode: str = "on_demand",
    block_policy_interval: str = "0s",
    statesync_enable: bool = False,
    statesync_rpc_servers: list[str] | None = None,
    statesync_trust_height: int = 0,
    statesync_trust_hash: str = "",
    statesync_trust_period: str = "168h0m0s",
    tracer_mode: str = "python_line_v1",
    execution_mode: str | None = None,
    execution_bytecode_version: str = "",
    execution_gas_schedule: str = "",
    execution_authority: str = "",
    metrics_enabled: bool = True,
    metrics_host: str = "127.0.0.1",
    metrics_port: int = 9108,
    metrics_bds_refresh_seconds: float = 5.0,
    transaction_trace_logging: bool = False,
    app_log_level: str = "INFO",
    app_log_json: bool = False,
    app_log_rotation_hours: int = 1,
    app_log_retention_days: int = 7,
    simulation_enabled: bool = True,
    simulation_max_concurrency: int = 2,
    simulation_timeout_ms: int = 3000,
    simulation_max_chi: int = 1_000_000,
    parallel_execution_enabled: bool = DEFAULT_PARALLEL_EXECUTION_ENABLED,
    parallel_execution_workers: int = DEFAULT_PARALLEL_EXECUTION_WORKERS,
    parallel_execution_min_transactions: int = (
        DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS
    ),
    pending_nonce_reservation_ttl_seconds: float = 60.0,
    max_pending_nonces_per_sender: int = 128,
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
    bds_spool_dir: str = "",
    bds_spool_warn_entries: int = 256,
    bds_spool_warn_bytes: int = 536_870_912,
    bds_disk_free_warn_bytes: int = 2_147_483_648,
) -> dict[str, str | list[str] | None]:
    if options is None:
        if moniker is None:
            raise TypeError("moniker is required when options is not provided")
        node_config = NodeConfigOptions(
            moniker=moniker,
            allow_cors=allow_cors,
            service_node=service_node,
            enable_pruning=enable_pruning,
            blocks_to_keep=blocks_to_keep,
            transaction_trace_logging=transaction_trace_logging,
            block_policy_mode=block_policy_mode,
            block_policy_interval=block_policy_interval,
            statesync=StateSyncOptions(
                enable=statesync_enable,
                rpc_servers=tuple(statesync_rpc_servers or ()),
                trust_height=statesync_trust_height,
                trust_hash=statesync_trust_hash,
                trust_period=statesync_trust_period,
            ),
            execution=ExecutionOptions(
                tracer_mode=tracer_mode,
                mode=execution_mode,
                bytecode_version=execution_bytecode_version,
                gas_schedule=execution_gas_schedule,
                authority=execution_authority,
            ),
            metrics=MetricsOptions(
                enabled=metrics_enabled,
                host=metrics_host,
                port=metrics_port,
                bds_refresh_seconds=metrics_bds_refresh_seconds,
            ),
            app_logging=AppLoggingOptions(
                level=app_log_level,
                json_logging=app_log_json,
                rotation_hours=app_log_rotation_hours,
                retention_days=app_log_retention_days,
            ),
            simulation=SimulationOptions(
                enabled=simulation_enabled,
                max_concurrency=simulation_max_concurrency,
                timeout_ms=simulation_timeout_ms,
                max_chi=simulation_max_chi,
            ),
            parallel_execution=ParallelExecutionOptions(
                enabled=parallel_execution_enabled,
                workers=parallel_execution_workers,
                min_transactions=parallel_execution_min_transactions,
            ),
            pending_nonce_reservation_ttl_seconds=(
                pending_nonce_reservation_ttl_seconds
            ),
            max_pending_nonces_per_sender=max_pending_nonces_per_sender,
            bds=BdsOptions(
                dsn=bds_dsn,
                host=bds_host,
                port=bds_port,
                database=bds_database,
                user=bds_user,
                password=bds_password,
                pool_min_size=bds_pool_min_size,
                pool_max_size=bds_pool_max_size,
                statement_timeout_ms=bds_statement_timeout_ms,
                application_name=bds_application_name,
                spool_dir=bds_spool_dir,
                spool_warn_entries=bds_spool_warn_entries,
                spool_warn_bytes=bds_spool_warn_bytes,
                disk_free_warn_bytes=bds_disk_free_warn_bytes,
            ),
            prometheus=prometheus,
        )
        options = ExistingHomeOptions(
            moniker=moniker,
            validator_private_key_hex=validator_private_key_hex,
            home=home,
            seed_node=seed_node,
            seed_node_address=seed_node_address,
            snapshot_url=snapshot_url,
            snapshot_signing_public_keys=tuple(
                snapshot_signing_public_keys or ()
            ),
            snapshot_expected_chain_id=snapshot_expected_chain_id,
            copy_genesis=copy_genesis,
            genesis_source=genesis_source,
            genesis_payload=genesis_payload,
            node_config=node_config,
        )

    request = options
    node_config = request.node_config or NodeConfigOptions(
        moniker=request.moniker
    )
    config_path = request.home / "config" / "config.toml"
    existing_config = load_existing_cometbft_config(config_path)
    seed_nodes = resolve_seed_nodes(
        seed_node=request.seed_node,
        seed_node_address=request.seed_node_address,
    )
    rendered_configs = render_node_configs(
        options=NodeConfigOptions(
            moniker=node_config.moniker,
            seed_nodes=tuple(seed_nodes),
            allow_cors=node_config.allow_cors,
            service_node=node_config.service_node,
            enable_pruning=node_config.enable_pruning,
            blocks_to_keep=node_config.blocks_to_keep,
            transaction_trace_logging=node_config.transaction_trace_logging,
            block_policy_mode=node_config.block_policy_mode,
            block_policy_interval=node_config.block_policy_interval,
            statesync=node_config.statesync,
            execution=node_config.execution,
            metrics=node_config.metrics,
            app_logging=node_config.app_logging,
            simulation=node_config.simulation,
            parallel_execution=node_config.parallel_execution,
            pending_nonce_reservation_ttl_seconds=(
                node_config.pending_nonce_reservation_ttl_seconds
            ),
            max_pending_nonces_per_sender=(
                node_config.max_pending_nonces_per_sender
            ),
            bds=node_config.bds,
            proxy_app=node_config.proxy_app,
            prometheus=node_config.prometheus,
        )
    )
    rendered_config = rendered_configs["cometbft"]
    xian_config = rendered_configs["xian"]
    config = preserve_runtime_config(rendered_config, existing_config)

    snapshot_archive_name: str | None = None
    if request.snapshot_url:
        snapshot_archive_name = apply_snapshot_archive(
            request.snapshot_url,
            request.home,
            trusted_manifest_public_keys=list(
                request.snapshot_signing_public_keys
            )
            or None,
            expected_chain_id=request.snapshot_expected_chain_id,
        )

    genesis_target_path: Path | None = None
    if request.copy_genesis:
        genesis_target_path = resolve_home_relative_path(
            request.home,
            config["genesis_file"],
        )
        genesis_target_path.parent.mkdir(parents=True, exist_ok=True)
        if request.genesis_payload is not None:
            write_json(
                genesis_target_path,
                request.genesis_payload,
                overwrite=True,
            )
        elif request.genesis_source is not None:
            genesis_source_path = resolve_genesis_source(request.genesis_source)
            shutil.copy2(genesis_source_path, genesis_target_path)
        else:
            raise ValueError(
                "genesis_source or genesis_payload is required when "
                "copy_genesis is enabled"
            )

    priv_validator_key_path: Path | None = None
    if request.validator_private_key_hex is not None:
        priv_validator_key_path = resolve_home_relative_path(
            request.home,
            config["priv_validator_key_file"],
        )
        priv_validator_key = build_priv_validator_key(
            request.validator_private_key_hex
        )
        priv_validator_key.pop("_private_key_hex", None)
        write_json(
            priv_validator_key_path,
            priv_validator_key,
            overwrite=True,
        )

    xian_config_path = request.home / "config" / "xian.toml"
    write_toml(config_path, config, overwrite=True)
    write_toml(xian_config_path, xian_config, overwrite=True)
    return {
        "config_path": str(config_path),
        "xian_config_path": str(xian_config_path),
        "genesis_path": (
            str(genesis_target_path) if genesis_target_path else None
        ),
        "priv_validator_key_path": (
            str(priv_validator_key_path) if priv_validator_key_path else None
        ),
        "snapshot_archive_name": snapshot_archive_name,
        "seed_nodes": list(seed_nodes),
    }
