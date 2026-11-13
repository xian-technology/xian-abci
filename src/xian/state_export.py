from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracting import constants as contracting_constants
from contracting.storage.driver import Driver
from xian_accounts import Ed25519Account
from xian_runtime_types.encoding import decode, encode

from xian.constants import Constants
from xian.execution_policy import (
    EXECUTION_POLICY_KEY,
    VM_EXECUTION_MODE,
    validate_execution_policy,
    validate_genesis_execution_policy,
    validate_persisted_execution_policy,
)
from xian.genesis_builder import hash_state_changes
from xian.state_root import (
    compute_driver_state_root,
    compute_exported_state_root,
    exported_state_items,
    is_consensus_state_key,
)
from xian.utils.block import (
    LATEST_BLOCK_DEFAULT,
    get_latest_block_hash,
    get_latest_block_height,
    get_latest_block_nanos,
    stage_latest_block,
    try_write_latest_block,
)


def fetch_filebased_state(
    *,
    storage_home: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Contract values and the durable block marker must share one read snapshot.
    with Driver(vm_profile=VM_EXECUTION_MODE, storage_home=storage_home) as driver:
        state = driver.items()
    contract_state = {key: value for key, value in state.items() if not key.startswith("__")}
    run_state = {key: value for key, value in state.items() if key.startswith("__")}
    return contract_state, run_state


def _hashed_run_state_entries(run_state: dict[str, Any], nonce_prefix: str) -> list[dict]:
    # Hashed run-state entries other than nonces (execution policy, runtime
    # features) travel in the genesis list, exactly as the app hash sees them.
    return [
        {"key": key, "value": value}
        for key, value in run_state.items()
        if value is not None
        and key.startswith("__")
        and is_consensus_state_key(key)
        and not key.startswith(nonce_prefix)
    ]


def build_exported_state(
    *,
    founder_private_key: str | None,
    contract_state: dict[str, Any],
    run_state: dict[str, Any],
    latest_block_hash: bytes | None = None,
    latest_block_height: int | None = None,
    latest_block_nanos: int | None = None,
    storage_home: Path | None = None,
) -> dict[str, Any]:
    execution_policy = run_state.get(EXECUTION_POLICY_KEY)
    validate_execution_policy(execution_policy)
    latest_app_hash = _given_or_stored(latest_block_hash, get_latest_block_hash, storage_home)
    block_height = _given_or_stored(latest_block_height, get_latest_block_height, storage_home)
    block_nanos = _given_or_stored(latest_block_nanos, get_latest_block_nanos, storage_home)

    exported_state = {
        "hash": "",
        "number": block_height,
        "nanos": block_nanos,
        "origin": {
            "signature": "",
            "sender": "",
        },
        "genesis": [],
    }

    nonce_prefix = f"__n{contracting_constants.INDEX_SEPARATOR}"
    nonces = [
        {"key": key[len(nonce_prefix) :], "value": value}
        for key, value in run_state.items()
        if key.startswith(nonce_prefix)
    ]
    nonces = sorted(nonces, key=lambda item: item["key"])

    for key, value in contract_state.items():
        if value is not None:
            exported_state["genesis"].append({"key": key, "value": value})
    exported_state["genesis"].extend(_hashed_run_state_entries(run_state, nonce_prefix))

    exported_state["genesis"] = sorted(
        exported_state["genesis"],
        key=lambda item: item["key"],
    )
    exported_state["nonces"] = nonces
    state_root = compute_exported_state_root(exported_state)
    if latest_app_hash and latest_app_hash != state_root:
        raise ValueError("exported state root does not match latest block app hash")
    exported_state["hash"] = state_root.hex()

    if founder_private_key:
        exported_state["origin"] = _signed_origin(founder_private_key, exported_state["genesis"])

    return exported_state


def _given_or_stored(value, read_stored, storage_home: Path | None):
    return value if value is not None else read_stored(storage_home)


def _signed_origin(founder_private_key: str, genesis: list[dict]) -> dict[str, str]:
    founder_wallet = Ed25519Account(founder_private_key)
    return {
        "signature": founder_wallet.sign_msg(hash_state_changes(genesis)),
        "sender": founder_wallet.public_key,
    }


def export_state(
    *,
    output_dir: Path,
    founder_private_key: str | None = None,
    storage_home: Path | None = None,
    output_filename: str = "exported_state.json",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    contract_state, run_state = fetch_filebased_state(storage_home=storage_home)
    latest_block = run_state.get(Constants.LATEST_BLOCK_KEY, LATEST_BLOCK_DEFAULT)
    exported_state = build_exported_state(
        founder_private_key=founder_private_key,
        contract_state=contract_state,
        run_state=run_state,
        latest_block_hash=bytes.fromhex(latest_block["hash"]),
        latest_block_height=latest_block["height"],
        latest_block_nanos=latest_block["nanos"],
        storage_home=storage_home,
    )

    output_path = output_dir / output_filename
    output_path.write_text(encode(exported_state, vm_profile=VM_EXECUTION_MODE), encoding="utf-8")
    return output_path


def load_exported_state(path: Path) -> dict[str, Any]:
    return decode(path.read_text(encoding="utf-8"), vm_profile=VM_EXECUTION_MODE)


def _import_state_writes(exported_state: dict[str, Any]) -> dict[str, Any]:
    writes: dict[str, Any] = {}
    for key, value in exported_state_items(exported_state):
        if key in writes:
            raise ValueError(f"exported state contains duplicate key: {key}")
        if not is_consensus_state_key(key):
            raise ValueError(f"exported state contains non-consensus key: {key}")
        writes[key] = value
    return writes


@dataclass(frozen=True)
class PreparedStateImport:
    state_root: bytes
    writes: dict[str, Any]
    height: int
    nanos: int


def prepare_state_import(exported_state: dict[str, Any]) -> PreparedStateImport:
    """Validate source state without opening or modifying destination storage."""
    validate_genesis_execution_policy(exported_state)
    state_root = compute_exported_state_root(exported_state)
    advertised_hash = bytes.fromhex(exported_state.get("hash", ""))
    if advertised_hash != state_root:
        raise ValueError("exported state root mismatch")
    return PreparedStateImport(
        state_root=state_root,
        writes=_import_state_writes(exported_state),
        height=int(exported_state.get("number", 0)),
        nanos=int(exported_state.get("nanos", 0)),
    )


def import_state(
    *,
    exported_state: dict[str, Any],
    storage_home: Path | None = None,
) -> dict[str, Any]:
    prepared = prepare_state_import(exported_state)
    resolved_storage_home = Path(storage_home) if storage_home else None
    validate_persisted_execution_policy(resolved_storage_home or contracting_constants.STORAGE_HOME)

    with Driver(vm_profile=VM_EXECUTION_MODE, storage_home=resolved_storage_home) as driver:
        # Replace stale keys and imported state in one atomic LMDB transaction.
        # Flushing first would destroy the previous state if import fails.
        driver.apply_writes({key: None for key in driver.keys_from_disk()})
        driver.apply_writes(prepared.writes)
        latest_block = stage_latest_block(
            driver,
            block_hash=prepared.state_root,
            height=prepared.height,
            nanos=prepared.nanos,
        )
        # State and its authoritative commit marker must become visible together.
        # Info reconciles the JSON mirror from this marker after import/restart.
        if compute_driver_state_root(driver) != prepared.state_root:
            raise ValueError("imported state root mismatch")
        driver.commit()
    try_write_latest_block(latest_block, resolved_storage_home)

    return {
        "height": prepared.height,
        "app_hash": prepared.state_root.hex(),
        "nanos": prepared.nanos,
        "keys_imported": len(prepared.writes),
    }
