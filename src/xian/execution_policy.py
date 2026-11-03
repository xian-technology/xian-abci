from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import lmdb
from contracting.storage.lmdb_environment import DEFAULT_ENVIRONMENT_POOL

VM_EXECUTION_MODE = "xian_vm_v1"
VM_BYTECODE_VERSION = "xvm-1"
VM_GAS_SCHEDULE = "xvm-gas-2"
VM_EXECUTION_REVISION = "xvm-execution-5"
SET_CODEC = "xian-set-1"
EXECUTION_POLICY_KEY = "__execution_policy"


def current_execution_policy() -> dict[str, str]:
    return {
        "execution_revision": VM_EXECUTION_REVISION,
        "gas_schedule": VM_GAS_SCHEDULE,
        "bytecode_version": VM_BYTECODE_VERSION,
        "set_codec": SET_CODEC,
    }


def validate_execution_policy(value) -> None:
    if not isinstance(value, Mapping) or dict(value) != current_execution_policy():
        raise ValueError(
            "incompatible or missing execution policy; use fresh current genesis "
            "or replay historical state with its pinned previous image"
        )


def validate_genesis_execution_policy(genesis: Mapping) -> None:
    state = genesis.get("abci_genesis", genesis)
    entries = state.get("genesis", []) if isinstance(state, Mapping) else []
    policies = [
        entry.get("value")
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("key") == EXECUTION_POLICY_KEY
    ]
    if len(policies) != 1:
        raise ValueError("genesis must contain exactly one execution policy")
    validate_execution_policy(policies[0])


def validate_persisted_execution_policy(storage_home: Path) -> bool:
    """Inspect existing state without creating directories or bootstrapping contracts."""
    path = Path(storage_home) / "lmdb"
    if not (path / "data.mdb").exists():
        return False
    existing = DEFAULT_ENVIRONMENT_POOL.read_existing_value(path, EXECUTION_POLICY_KEY.encode())
    if existing is None:
        with lmdb.open(str(path), readonly=True, create=False, lock=False) as env:
            with env.begin() as tx:
                existing = tx.get(EXECUTION_POLICY_KEY.encode()), tx.stat()["entries"]
    raw, entries = existing
    if entries == 0:
        return False
    try:
        value = json.loads(raw) if raw is not None else None
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid persisted execution policy") from exc
    validate_execution_policy(value)
    return True


def initialize_execution_policy(storage_home: Path) -> None:
    """Mark only empty state before ContractingClient commits its local helpers."""
    if validate_persisted_execution_policy(storage_home):
        return
    path = Path(storage_home) / "lmdb"
    path.mkdir(parents=True, exist_ok=True)
    with lmdb.open(str(path)) as env:
        with env.begin(write=True) as tx:
            if tx.stat()["entries"]:
                raise ValueError("state changed during execution policy initialization")
            encoded = json.dumps(current_execution_policy(), separators=(",", ":")).encode()
            tx.put(EXECUTION_POLICY_KEY.encode(), encoded)


def validate_vm_execution_mode(*, mode: str | None = None) -> None:
    selected = (mode or VM_EXECUTION_MODE).strip()
    if selected != VM_EXECUTION_MODE:
        raise ValueError("xian_vm_v1 is the only supported execution mode")


def validate_vm_execution_config(
    xian_config: dict[str, object] | None,
) -> None:
    payload = xian_config or {}
    if "tracer_mode" in payload:
        raise ValueError("xian.tracer_mode is no longer supported; Xian nodes only run xian_vm_v1")
    if "execution" in payload:
        raise ValueError("xian.execution has been removed; Xian nodes only run xian_vm_v1")
    validate_vm_execution_mode()
