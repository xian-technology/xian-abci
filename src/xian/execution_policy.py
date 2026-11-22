from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from contracting.storage.lmdb_environment import (
    DEFAULT_ENVIRONMENT_POOL,
    standalone_environment,
)

VM_EXECUTION_MODE = "xian_vm_v2"
VM_BYTECODE_VERSION = "xvm-2"
VM_GAS_SCHEDULE = "xvm-gas-7"
VM_EXECUTION_REVISION = "xvm-execution-15"
SET_CODEC = "xian-set-1"
# Contract admission (compiler-core) and transaction admission (fastpath-core,
# linked into vm-core) are consensus rules like execution.
COMPILER_ADMISSION_REVISION = "xian-admission-4"
TX_ADMISSION_REVISION = "xian-fastpath-admission-3"
EXECUTION_POLICY_KEY = "__execution_policy"


def current_execution_policy() -> dict[str, str]:
    return {
        "execution_revision": VM_EXECUTION_REVISION,
        "gas_schedule": VM_GAS_SCHEDULE,
        "bytecode_version": VM_BYTECODE_VERSION,
        "set_codec": SET_CODEC,
        "compiler_admission_revision": COMPILER_ADMISSION_REVISION,
        "tx_admission_revision": TX_ADMISSION_REVISION,
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
        with standalone_environment(path, readonly=True, lock=False) as env:
            existing = env.get_with_entries(EXECUTION_POLICY_KEY.encode())
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
    encoded = json.dumps(current_execution_policy(), separators=(",", ":")).encode()
    with standalone_environment(path) as env:
        if not env.write_if_empty([(EXECUTION_POLICY_KEY.encode(), encoded)]):
            raise ValueError("state changed during execution policy initialization")
