from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit, urlunsplit
from urllib.request import urlopen

from xian.config_paths import resolve_genesis_source
from xian.constants import Constants as c
from xian.full_node_store import (
    NativeStoreInspector,
    canonical_hex,
    requires_pq_snapshot_publisher,
    verify_full_node_store_manifest,
)
from xian.full_node_store_limits import MAX_MANIFEST_BYTES, FullNodeStoreLimits
from xian.node_setup import (
    NodeConfigOptions,
    build_priv_validator_key,
    genesis_sha256,
    load_genesis,
    render_node_configs,
    verify_genesis_sha256,
    write_json,
    write_toml,
)
from xian.snapshot_attestation import is_pq_pin, validate_publisher_pins
from xian.snapshot_restore import _restore_authenticated_archive
from xian.state_sync_policy import validate_comet_state_sync_disabled
from xian.toml_utils import load as load_toml
from xian.validator_keys import ValidatorSuite
from xian.utils.cometbft import format_url_netloc, normalize_rpc_url, split_host_port

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
_DEFAULT_DISCOVERY_RPC_PORT = 26657
_DEFAULT_DISCOVERY_P2P_PORT = 26656


@dataclass(frozen=True, slots=True)
class ExistingHomeOptions:
    moniker: str
    validator_private_key_hex: str | None = None
    home: Path = c.COMETBFT_HOME
    discover_seeds: tuple[str, ...] = ()
    p2p_seeds: tuple[str, ...] = ()
    p2p_persistent_peers: tuple[str, ...] = ()
    snapshot_url: str | None = None
    snapshot_signing_public_keys: tuple[str, ...] = ()
    snapshot_expected_chain_id: str | None = None
    native_store_inspector: NativeStoreInspector | None = None
    genesis_source: str | None = None
    genesis_payload: dict[str, Any] | None = None
    expected_genesis_sha256: str | None = None
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


def _seed_host_port(seed_node: str) -> tuple[str, str | None]:
    seed = seed_node.strip()
    if "://" in seed:
        parsed = urlsplit(normalize_rpc_url(seed))
        port = str(parsed.port) if parsed.port is not None else None
        return parsed.hostname or "", port
    return split_host_port(seed)


def _seed_rpc_status_url(seed_node: str) -> str:
    seed = seed_node.strip()
    scheme = "http"
    if "://" in seed:
        parsed = urlsplit(normalize_rpc_url(seed))
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        port = str(parsed.port) if parsed.port is not None else None
    else:
        host, port = split_host_port(seed)
    return urlunsplit(
        (
            scheme,
            format_url_netloc(host, port or _DEFAULT_DISCOVERY_RPC_PORT),
            "status",
            "",
            "",
        )
    )


def _p2p_seed_address(node_id: str, seed_node: str) -> str:
    host, _port = _seed_host_port(seed_node)
    return f"{node_id}@{format_url_netloc(host, _DEFAULT_DISCOVERY_P2P_PORT)}"


def _normalize_p2p_seed(seed: str) -> str:
    node_id, separator, address = seed.rpartition("@")
    if not separator:
        return seed

    host, port = split_host_port(address)
    if not host or port is None:
        return seed
    return f"{node_id}@{format_url_netloc(host, port)}"


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
                _seed_rpc_status_url(seed_node),
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


def resolve_p2p_seeds(
    *,
    discover_seeds: tuple[str, ...] = (),
    p2p_seeds: tuple[str, ...] = (),
) -> list[str]:
    resolved = [_normalize_p2p_seed(seed) for seed in p2p_seeds]
    for seed in discover_seeds:
        status = fetch_seed_node_status(seed)
        if status is None:
            raise RuntimeError(f"failed to get node information from seed node {seed}")

        node_id = status["result"]["node_info"]["id"]
        resolved.append(_p2p_seed_address(node_id, seed))
    return resolved


def _download_snapshot_archive(
    snapshot_url: str, target_path: Path, *, limits=None, expected_bytes=None, expected_sha256=None
) -> Path:
    limits = limits or FullNodeStoreLimits()
    filename = Path(urlparse(snapshot_url).path).name or "snapshot.tar.gz"
    archive_path = target_path / filename
    maximum = limits.max_archive_bytes
    if expected_bytes is not None:
        limits.check_size(expected_bytes, maximum, "archive")
        limits.require_disk_space(target_path, expected_bytes)
        maximum = expected_bytes
    with urlopen(snapshot_url, timeout=30) as response:
        length = response.headers.get("Content-Length")
        if length is not None:
            limits.check_size(int(length), maximum, "advertised archive")
            if expected_bytes is not None and int(length) != expected_bytes:
                raise ValueError("full-node-store advertised archive byte count mismatch")
        _, actual = limits.copy_stream(
            response, archive_path, maximum=maximum, expected_bytes=expected_bytes, label="archive"
        )
    if actual != expected_sha256:
        raise ValueError(
            f"snapshot archive sha256 mismatch: expected {expected_sha256}, got {actual}"
        )
    return archive_path


def _fetch_json_url(url: str, *, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("snapshot manifest/JSON response exceeds 64KiB limit")
    return json.loads(payload.decode(charset))


def _snapshot_source(snapshot_url, trusted_keys, expected_sha256, expected_chain_id):
    if trusted_keys:
        try:
            fetched = _fetch_json_url(snapshot_url, timeout=30)
        except (ValueError, UnicodeError):
            if is_pq_pin(trusted_keys[0]):
                raise ValueError("ML-DSA-65 snapshot pins require a signed v3 manifest") from None
            if expected_sha256 is None:
                raise
            # Only an independently supplied checksum permits a non-JSON URI.
            return snapshot_url, expected_sha256, None
        manifest = verify_full_node_store_manifest(
            fetched, trusted_manifest_public_keys=trusted_keys, expected_chain_id=expected_chain_id
        )
        if expected_sha256 is not None and manifest["snapshot_sha256"] != expected_sha256:
            raise ValueError("snapshot archive sha256 mismatch with trusted manifest")
        return manifest["snapshot_url"], manifest["snapshot_sha256"], manifest
    if expected_sha256 is None:
        raise ValueError(
            "remote snapshot restore requires expected_sha256 or trusted snapshot signing keys"
        )
    return snapshot_url, expected_sha256, None


def apply_snapshot_archive(
    snapshot_url: str | Path,
    home: Path,
    *,
    expected_sha256: str | None = None,
    trusted_manifest_public_keys: list[str] | None = None,
    expected_chain_id: str | None = None,
    native_store_inspector: NativeStoreInspector | None = None,
    limits: FullNodeStoreLimits | None = None,
) -> str:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    limits = limits or FullNodeStoreLimits()
    if expected_sha256 is not None:
        canonical_hex(expected_sha256, "trusted snapshot SHA256")
    trusted_keys = validate_publisher_pins(trusted_manifest_public_keys or [])
    if not (trusted_keys and is_pq_pin(trusted_keys[0])) and requires_pq_snapshot_publisher(home):
        raise ValueError("this home requires pinned ML-DSA-65 snapshot publishers")
    if isinstance(snapshot_url, Path) or not urlsplit(snapshot_url).scheme:
        snapshot_url = Path(snapshot_url).expanduser().resolve().as_uri()
    url, checksum, manifest = _snapshot_source(
        snapshot_url, trusted_keys, expected_sha256, expected_chain_id
    )
    if manifest is not None:
        limits.check_size(
            manifest["uncompressed_bytes"], limits.max_uncompressed_bytes, "uncompressed archive"
        )
        limits.check_size(manifest["snapshot_bytes"], limits.max_archive_bytes, "archive")
        limits.require_disk_space(
            home, manifest["snapshot_bytes"] + 2 * manifest["uncompressed_bytes"]
        )
    with tempfile.TemporaryDirectory(dir=home) as tmp_dir:
        archive_path = _download_snapshot_archive(
            url,
            Path(tmp_dir),
            limits=limits,
            expected_bytes=manifest["snapshot_bytes"] if manifest else None,
            expected_sha256=checksum,
        )
        _restore_authenticated_archive(
            archive_path,
            home,
            pq_authenticated=manifest is not None and manifest["manifest_version"] == 3,
            manifest=manifest,
            native_store_inspector=native_store_inspector,
            expected_chain_id=expected_chain_id,
            limits=limits,
        )
        return archive_path.name


def resolve_home_relative_path(home: Path, configured_path: str) -> Path:
    path = Path(configured_path)
    if path.is_absolute():
        return path
    return home / path


def configure_existing_home(request: ExistingHomeOptions) -> dict[str, str | list[str] | None]:
    node_config = request.node_config or NodeConfigOptions(moniker=request.moniker)
    config_path = request.home / "config" / "config.toml"
    existing_config = load_existing_cometbft_config(config_path)
    validate_comet_state_sync_disabled(existing_config)
    p2p_seeds = resolve_p2p_seeds(
        discover_seeds=request.discover_seeds,
        p2p_seeds=request.p2p_seeds,
    )
    rendered_configs = render_node_configs(
        options=replace(
            node_config,
            p2p_seeds=tuple(p2p_seeds),
            p2p_persistent_peers=request.p2p_persistent_peers,
        )
    )
    rendered_config = rendered_configs["cometbft"]
    xian_config = rendered_configs["xian"]
    config = preserve_runtime_config(rendered_config, existing_config)
    _verify_genesis_pin(request, config["genesis_file"])

    snapshot_archive_name, genesis_target_path = _restore_with_genesis(
        request, config["genesis_file"]
    )
    priv_validator_key_path = _install_validator_key(request, config["priv_validator_key_file"])

    xian_config_path = request.home / "config" / "xian.toml"
    write_toml(config_path, config, overwrite=True)
    write_toml(xian_config_path, xian_config, overwrite=True, private=True)
    return {
        "config_path": str(config_path),
        "xian_config_path": str(xian_config_path),
        "genesis_path": (str(genesis_target_path) if genesis_target_path else None),
        "priv_validator_key_path": (
            str(priv_validator_key_path) if priv_validator_key_path else None
        ),
        "snapshot_archive_name": snapshot_archive_name,
        "p2p_seeds": list(p2p_seeds),
        "p2p_persistent_peers": list(request.p2p_persistent_peers),
    }


def _restore_with_genesis(request, genesis_file):
    target = resolve_home_relative_path(request.home, genesis_file)
    previous = target.read_bytes() if target.exists() else None
    installed = _install_genesis(request, genesis_file)
    try:
        return _restore_snapshot(request), installed
    except BaseException:
        if installed is not None:
            if previous is None:
                target.unlink()
            else:
                target.write_bytes(previous)
        raise


def _restore_snapshot(request: ExistingHomeOptions) -> str | None:
    if not request.snapshot_url:
        return None
    return apply_snapshot_archive(
        request.snapshot_url,
        request.home,
        trusted_manifest_public_keys=list(request.snapshot_signing_public_keys) or None,
        expected_chain_id=request.snapshot_expected_chain_id,
        native_store_inspector=request.native_store_inspector,
    )


def _verify_genesis_pin(request: ExistingHomeOptions, genesis_file: str) -> None:
    """Refuse, before anything is written, a home whose genesis would differ from the pin."""
    installed_path = resolve_home_relative_path(request.home, genesis_file)
    if request.genesis_payload is not None:
        genesis = request.genesis_payload
    elif request.genesis_source is not None:
        genesis = load_genesis(resolve_genesis_source(request.genesis_source))
    elif request.expected_genesis_sha256 is None:
        return
    else:
        genesis = load_genesis(installed_path)
    if installed_path.exists() and ValidatorSuite.from_genesis(load_genesis(installed_path)) is not ValidatorSuite.from_genesis(genesis):
        raise ValueError("changing a validator suite requires a fresh node home")
    verify_genesis_sha256(genesis_sha256(genesis), request.expected_genesis_sha256)


def _install_genesis(request: ExistingHomeOptions, genesis_file: str) -> Path | None:
    if request.genesis_payload is None and request.genesis_source is None:
        return None
    genesis_target_path = resolve_home_relative_path(request.home, genesis_file)
    genesis_target_path.parent.mkdir(parents=True, exist_ok=True)
    if request.genesis_payload is not None:
        write_json(genesis_target_path, request.genesis_payload, overwrite=True)
    else:
        shutil.copy2(resolve_genesis_source(request.genesis_source), genesis_target_path)
    return genesis_target_path


def _install_validator_key(request: ExistingHomeOptions, key_file: str) -> Path | None:
    if request.validator_private_key_hex is None:
        return None
    priv_validator_key_path = resolve_home_relative_path(request.home, key_file)
    priv_validator_key = build_priv_validator_key(request.validator_private_key_hex)
    priv_validator_key.pop("_private_key_hex", None)
    write_json(priv_validator_key_path, priv_validator_key, overwrite=True, private=True)
    return priv_validator_key_path
