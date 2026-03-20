from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from xian.config_paths import (
    resolve_genesis_source,
)
from xian.constants import Constants as c
from xian.node_setup import (
    build_priv_validator_key,
    render_cometbft_config,
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


def apply_snapshot_archive(snapshot_url: str, home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home) as tmp_dir:
        archive_path = _download_snapshot_archive(snapshot_url, Path(tmp_dir))
        if not tarfile.is_tarfile(archive_path):
            raise ValueError("snapshot archive must be a .tar or .tar.gz file")

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
    moniker: str,
    validator_private_key_hex: str | None,
    home: Path = c.COMETBFT_HOME,
    allow_cors: bool = True,
    seed_node: str | None = None,
    seed_node_address: str | None = None,
    snapshot_url: str | None = None,
    copy_genesis: bool = False,
    genesis_source: str | None = None,
    prometheus: bool = True,
    service_node: bool = False,
    enable_pruning: bool = False,
    blocks_to_keep: int = 100000,
    block_policy_mode: str = "on_demand",
    block_policy_interval: str = "0s",
    tracer_mode: str = "python_line_v1",
    metrics_enabled: bool = True,
    metrics_host: str = "127.0.0.1",
    metrics_port: int = 9108,
    metrics_bds_refresh_seconds: float = 5.0,
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
    bds_spool_dir: str = "",
    bds_spool_warn_entries: int = 256,
    bds_spool_warn_bytes: int = 536_870_912,
    bds_disk_free_warn_bytes: int = 2_147_483_648,
) -> dict[str, str | list[str] | None]:
    config_path = home / "config" / "config.toml"
    existing_config = load_existing_cometbft_config(config_path)
    seed_nodes = resolve_seed_nodes(
        seed_node=seed_node,
        seed_node_address=seed_node_address,
    )
    rendered_config = render_cometbft_config(
        moniker=moniker,
        seed_nodes=seed_nodes,
        allow_cors=allow_cors,
        service_node=service_node,
        enable_pruning=enable_pruning,
        blocks_to_keep=blocks_to_keep,
        block_policy_mode=block_policy_mode,
        block_policy_interval=block_policy_interval,
        tracer_mode=tracer_mode,
        metrics_enabled=metrics_enabled,
        metrics_host=metrics_host,
        metrics_port=metrics_port,
        metrics_bds_refresh_seconds=metrics_bds_refresh_seconds,
        parallel_execution_enabled=parallel_execution_enabled,
        parallel_execution_workers=parallel_execution_workers,
        parallel_execution_min_transactions=(
            parallel_execution_min_transactions
        ),
        bds_dsn=bds_dsn,
        bds_host=bds_host,
        bds_port=bds_port,
        bds_database=bds_database,
        bds_user=bds_user,
        bds_password=bds_password,
        bds_pool_min_size=bds_pool_min_size,
        bds_pool_max_size=bds_pool_max_size,
        bds_statement_timeout_ms=bds_statement_timeout_ms,
        bds_application_name=bds_application_name,
        bds_spool_dir=bds_spool_dir,
        bds_spool_warn_entries=bds_spool_warn_entries,
        bds_spool_warn_bytes=bds_spool_warn_bytes,
        bds_disk_free_warn_bytes=bds_disk_free_warn_bytes,
        prometheus=prometheus,
    )
    config = preserve_runtime_config(rendered_config, existing_config)

    snapshot_archive_name: str | None = None
    if snapshot_url:
        snapshot_archive_name = apply_snapshot_archive(snapshot_url, home)

    genesis_target_path: Path | None = None
    if copy_genesis:
        if genesis_source is not None:
            genesis_source_path = resolve_genesis_source(genesis_source)
        else:
            raise ValueError(
                "genesis_source is required when copy_genesis is enabled"
            )
        genesis_target_path = resolve_home_relative_path(
            home,
            config["genesis_file"],
        )
        genesis_target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(genesis_source_path, genesis_target_path)

    priv_validator_key_path: Path | None = None
    if validator_private_key_hex is not None:
        priv_validator_key_path = resolve_home_relative_path(
            home,
            config["priv_validator_key_file"],
        )
        priv_validator_key = build_priv_validator_key(validator_private_key_hex)
        priv_validator_key.pop("_private_key_hex", None)
        write_json(
            priv_validator_key_path,
            priv_validator_key,
            overwrite=True,
        )

    write_toml(config_path, config, overwrite=True)
    return {
        "config_path": str(config_path),
        "genesis_path": (
            str(genesis_target_path) if genesis_target_path else None
        ),
        "priv_validator_key_path": (
            str(priv_validator_key_path) if priv_validator_key_path else None
        ),
        "snapshot_archive_name": snapshot_archive_name,
        "seed_nodes": list(seed_nodes),
    }
