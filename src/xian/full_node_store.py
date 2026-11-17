"""Authenticate coherent native Comet and Xian checkpoints before publication.

A trusted backup publisher (or independently pinned archive SHA) is the trust
anchor. Native inspection proves store consistency and complete live evidence
metadata; this is not replay verification from genesis.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from contracting.storage.lmdb_environment import standalone_environment
from xian_accounts import verify_message
from xian_runtime_types.encoding import decode
from xian_vm_core import StateRootTree

from xian.checkpoint_validation import (
    canonical_hex as canonical_hex,
)
from xian.checkpoint_validation import (
    canonical_integer as canonical_integer,
)
from xian.checkpoint_validation import (
    positive_integer as positive_integer,
)
from xian.evidence_policy import (
    TIMESTAMP_MAX_NS,
    TIMESTAMP_MIN_NS,
    committed_evidence_policy_response,
    validate_committed_evidence_policy,
)
from xian.execution_policy import EXECUTION_POLICY_KEY, validate_execution_policy
from xian.full_node_store_limits import FullNodeStoreLimits  # noqa: F401 - public policy API
from xian.node_setup import genesis_sha256, load_genesis
from xian.snapshot_restore_status import validate_snapshot_restore_complete
from xian.state_root import HASHED_NODE_KEYS, HASHED_NODE_PREFIXES
from xian.toml_utils import load as load_toml

NativeStoreInspector = Callable[[Path, str, Path], dict]
MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "snapshot_kind",
        "chain_id",
        "height",
        "genesis_sha256",
        "app_hash",
        "block_hash",
        "snapshot_url",
        "snapshot_sha256",
        "snapshot_bytes",
        "uncompressed_bytes",
        "signing_public_key",
        "signature",
    }
)
CHECKPOINT_FIELDS = ("chain_id", "height", "genesis_sha256", "app_hash", "block_hash")
INSPECTOR_FIELDS = frozenset(
    {
        "schema_version",
        "chain_id",
        "height",
        "app_hash",
        "block_hash",
        "last_block_nanos",
        "evidence",
        "block_store_base",
        "block_store_height",
        "history_first_verified",
        "history_first_nanos",
        "initial_height",
    }
)


def validate_full_node_store_manifest(manifest: dict) -> dict:
    if type(manifest) is not dict or set(manifest) != MANIFEST_FIELDS:
        raise ValueError("full-node-store manifest manifest_version 2 requires the exact fields")
    if type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 2:
        raise ValueError("unsupported snapshot manifest_version: full-node-store requires v2")
    if manifest["snapshot_kind"] != "full_node_store":
        raise ValueError("snapshot_kind must be full_node_store")
    _validate_manifest_fields(manifest)
    return dict(manifest)


def _validate_manifest_fields(manifest):
    for key in ("chain_id", "snapshot_url"):
        if type(manifest[key]) is not str or not manifest[key] or len(manifest[key]) > 8192:
            raise ValueError(f"snapshot manifest {key} must be a nonempty bounded string")
    for key in ("height", "snapshot_bytes", "uncompressed_bytes"):
        positive_integer(manifest[key], key)
    for key in (
        "genesis_sha256",
        "app_hash",
        "block_hash",
        "snapshot_sha256",
        "signing_public_key",
    ):
        canonical_hex(manifest[key], key)
    canonical_hex(manifest["signature"], "signature", length=128)


def full_node_store_manifest_payload(manifest: dict) -> str:
    # Signing callers have not added a signature yet; validate through the same
    # schema with a placeholder, then exclude only that field from the domain.
    checked = validate_full_node_store_manifest({**manifest, "signature": "0" * 128})
    return json.dumps(
        {key: value for key, value in checked.items() if key != "signature"},
        sort_keys=True,
        separators=(",", ":"),
    )


def verify_full_node_store_manifest(
    manifest: dict, *, trusted_manifest_public_keys: list[str], expected_chain_id: str | None = None
) -> dict:
    checked = validate_full_node_store_manifest(manifest)
    if expected_chain_id is not None and checked["chain_id"] != expected_chain_id:
        raise ValueError("snapshot manifest chain_id mismatch")
    if checked["signing_public_key"] not in trusted_manifest_public_keys:
        raise ValueError("snapshot manifest signer is not trusted")
    if not verify_message(
        checked["signing_public_key"],
        full_node_store_manifest_payload(checked),
        checked["signature"],
    ):
        raise ValueError("snapshot manifest signature verification failed")
    return checked


def _read_inspector_process(command: list[str], *, timeout: float = 130) -> dict:
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
        payload = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ValueError("native full-node-store inspection timed out")
                    chunk = os.read(child.stdout.fileno(), min(8192, 65537 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                    if len(payload) > 65536:
                        raise ValueError("native full-node-store inspection output exceeds limit")
            if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                raise ValueError("native full-node-store inspection failed")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    return json.loads(payload)


@dataclass(frozen=True)
class NativeStoreInspectionLimits:
    max_history_heights: int = 1_000_000
    timeout_seconds: int = 120

    def __post_init__(self):
        positive_integer(self.max_history_heights, "inspector max_history_heights")
        positive_integer(self.timeout_seconds, "inspector timeout_seconds")

    @classmethod
    def from_environment(cls):
        return cls(
            **{
                key: canonical_integer(os.environ[env], env)
                for key, env in (
                    ("max_history_heights", "XIAN_NODE_STORE_INSPECT_MAX_HISTORY_HEIGHTS"),
                    ("timeout_seconds", "XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS"),
                )
                if env in os.environ
            }
        )

    def command_arguments(self):
        return [
            "--max-history-heights",
            str(self.max_history_heights),
            "--timeout",
            str(self.timeout_seconds) + "s",
        ]


def inspect_native_store(db_dir: Path, db_backend: str, genesis_file: Path) -> dict:
    if db_backend != "goleveldb":
        raise ValueError("verified full-node-store snapshots support only goleveldb")
    limits = NativeStoreInspectionLimits.from_environment()
    try:
        return _read_inspector_process(
            [
                "xian-node-store-inspect",
                "--db-dir",
                str(db_dir),
                "--db-backend",
                db_backend,
                "--genesis-file",
                str(genesis_file),
                *limits.command_arguments(),
            ],
            timeout=limits.timeout_seconds + 10,
        )
    except FileNotFoundError as error:
        raise ValueError(
            "xian-node-store-inspect is required for verified full-node-store restore"
        ) from error


class _ReadonlyState:
    def __init__(self, environment):
        self.environment = environment

    def value_from_disk(self, key):
        raw = self.environment.get(key.encode())
        return None if raw is None else decode(raw, vm_profile="xian_vm_v2")


def _application_checkpoint(home: Path) -> dict:
    lmdb = home / "xian/lmdb"
    if not (lmdb / "data.mdb").is_file():
        raise ValueError("full-node-store checkpoint is missing its Xian LMDB")
    with standalone_environment(lmdb, readonly=True, lock=False) as environment:
        state = _ReadonlyState(environment)
        validate_execution_policy(state.value_from_disk(EXECUTION_POLICY_KEY))
        validate_committed_evidence_policy(state)
        point = committed_evidence_policy_response(state)
        root = StateRootTree.from_storage(
            environment, list(HASHED_NODE_KEYS), list(HASHED_NODE_PREFIXES)
        )
        if root.root_hash.hex() != point["app_hash"]:
            raise ValueError("full-node-store checkpoint application root mismatch")
    return point


def _reference_config(home: Path) -> tuple[dict, dict, Path]:
    config_path = home / "config/config.toml"
    if not config_path.is_file():
        raise ValueError("full-node-store verification requires local config and genesis")
    with config_path.open() as file:
        config = load_toml(file)
    genesis_path = Path(config.get("genesis_file", "config/genesis.json"))
    genesis_path = genesis_path if genesis_path.is_absolute() else home / genesis_path
    genesis = load_genesis(genesis_path)
    return config, genesis, genesis_path


def _candidate_db_dir(store_home: Path, reference_home: Path, config: dict) -> Path:
    configured = Path(config.get("db_dir", "data"))
    absolute = configured if configured.is_absolute() else reference_home / configured
    try:
        relative = absolute.resolve().relative_to((reference_home / "data").resolve())
    except ValueError as error:
        raise ValueError("verified full-node-store db_dir must be inside the data root") from error
    return store_home / "data" / relative


def _native_checkpoint(native: dict) -> dict:
    if type(native) is not dict or set(native) != INSPECTOR_FIELDS:
        raise ValueError("native full-node-store checkpoint has noncanonical fields")
    if type(native["schema_version"]) is not int or native["schema_version"] != 1:
        raise ValueError("unsupported native full-node-store checkpoint schema")
    result = dict(native)
    for key in (
        "height",
        "block_store_base",
        "block_store_height",
        "history_first_verified",
        "initial_height",
    ):
        result[key] = canonical_integer(native[key], key)
    for key in ("last_block_nanos", "history_first_nanos"):
        result[key] = canonical_integer(
            native[key], key, minimum=TIMESTAMP_MIN_NS, maximum=TIMESTAMP_MAX_NS
        )
    for key in ("app_hash", "block_hash"):
        canonical_hex(native[key], key)
    result["evidence"] = _native_evidence(native["evidence"])
    return result


def _native_evidence(evidence):
    if type(evidence) is not dict or set(evidence) != {
        "max_age_num_blocks",
        "max_age_duration",
        "max_bytes",
    }:
        raise ValueError("native checkpoint must contain full evidence limits")
    return {
        key: canonical_integer(value, key, minimum=0 if key == "max_bytes" else 1)
        for key, value in evidence.items()
    }


def _validate_checkpoint_binding(point: dict, native: dict, genesis: dict) -> None:
    if native["chain_id"] != genesis["chain_id"]:
        raise ValueError("full-node-store chain_id mismatch")
    if str(native["height"]) != point["height"] or native["block_store_height"] != native["height"]:
        raise ValueError("full-node-store native and application height mismatch")
    if (
        native["app_hash"] != point["app_hash"]
        or str(native["last_block_nanos"]) != point["last_block_nanos"]
    ):
        raise ValueError("full-node-store native and application root/time mismatch")
    if {key: str(value) for key, value in native["evidence"].items()} != point["evidence"]:
        raise ValueError("full-node-store native and application evidence limits mismatch")
    _validate_history_boundary(native, genesis)


def _validate_history_boundary(native: dict, genesis: dict) -> None:
    initial = int(genesis.get("initial_height", "0")) or 1
    first, height = native["history_first_verified"], native["height"]
    if native["initial_height"] != initial or not initial <= first <= height:
        raise ValueError("full-node-store evidence history initial height mismatch")
    if (
        not initial <= native["block_store_base"] <= height
        or native["history_first_nanos"] > native["last_block_nanos"]
    ):
        raise ValueError("full-node-store evidence history boundary is invalid")
    if first == initial:
        return
    limits = native["evidence"]
    if not (
        height - first > limits["max_age_num_blocks"]
        and native["last_block_nanos"] - native["history_first_nanos"] > limits["max_age_duration"]
    ):
        raise ValueError("full-node-store evidence history does not cover the complete live window")


def read_full_node_store_checkpoint(
    store_home: Path,
    *,
    reference_home: Path | None = None,
    native_store_inspector: NativeStoreInspector | None = None,
) -> dict:
    store_home = Path(store_home).resolve()
    reference_home = Path(reference_home or store_home).resolve()
    validate_snapshot_restore_complete(store_home)
    config, genesis, genesis_path = _reference_config(reference_home)
    point = _application_checkpoint(store_home)
    inspect = native_store_inspector or inspect_native_store
    native = _native_checkpoint(
        inspect(
            _candidate_db_dir(store_home, reference_home, config),
            config.get("db_backend", "goleveldb"),
            genesis_path,
        )
    )
    _validate_checkpoint_binding(point, native, genesis)
    return {
        "chain_id": genesis["chain_id"],
        "height": native["height"],
        "genesis_sha256": genesis_sha256(genesis),
        "app_hash": point["app_hash"],
        "block_hash": native["block_hash"],
    }


def verify_full_node_store_checkpoint(
    store_home: Path,
    manifest: dict,
    *,
    reference_home: Path | None = None,
    native_store_inspector: NativeStoreInspector | None = None,
) -> dict:
    point = read_full_node_store_checkpoint(
        store_home, reference_home=reference_home, native_store_inspector=native_store_inspector
    )
    for key in CHECKPOINT_FIELDS:
        if point[key] != manifest[key]:
            raise ValueError(f"full-node-store checkpoint {key} mismatch")
    return point
