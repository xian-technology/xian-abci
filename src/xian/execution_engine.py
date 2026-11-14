"""The node's native VM runtime: the startup checks that the installed
vm-core and compiler implement the node's execution and admission rules,
and the VM context of an external call. Transactions run in Rust
(``xian_vm_core.BlockExecutor`` for blocks, ``execute_tx`` for one call)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from xian.execution_policy import (
    COMPILER_ADMISSION_REVISION,
    SET_CODEC,
    TX_ADMISSION_REVISION,
    VM_BYTECODE_VERSION,
    VM_EXECUTION_MODE,
    VM_EXECUTION_REVISION,
    VM_GAS_SCHEDULE,
)


@dataclass(frozen=True, slots=True)
class VmRuntime:
    runtime_info: dict[str, Any] | None = None

    @property
    def mode(self) -> str:
        return VM_EXECUTION_MODE


class ContractPreparationError(ValueError):
    """A contract cannot run in current chain state; the message is canonical."""


@lru_cache(maxsize=1)
def _load_vm_runtime_bindings():
    try:
        import xian_vm_core
    except ImportError as exc:  # pragma: no cover - exercised via unit tests
        raise ValueError(
            "execution engine 'xian_vm_v2' requires the native "
            "'xian-tech-vm-core' package to be installed"
        ) from exc
    return xian_vm_core


def build_vm_runtime() -> VmRuntime:
    bindings = _load_vm_runtime_bindings()
    runtime_info = bindings.runtime_info(VM_EXECUTION_MODE)
    if (
        runtime_info.get("execution_revision") != VM_EXECUTION_REVISION
        or runtime_info.get("set_codec") != SET_CODEC
    ):
        raise ValueError("native runtime execution policy does not match the node")
    if runtime_info.get("vm_profile") != VM_EXECUTION_MODE:
        raise ValueError(
            "xian_vm_v2 native runtime reported an unexpected vm_profile: "
            f"{runtime_info.get('vm_profile')!r}"
        )
    if not bindings.supports_execution_policy(
        VM_BYTECODE_VERSION,
        VM_GAS_SCHEDULE,
        VM_EXECUTION_MODE,
    ):
        raise ValueError(
            "xian_vm_v2 native runtime does not support execution policy "
            f"bytecode_version={VM_BYTECODE_VERSION!r} "
            f"gas_schedule={VM_GAS_SCHEDULE!r}"
        )

    validate_admission_runtimes(runtime_info)
    return VmRuntime(runtime_info=runtime_info)


_LOADED = object()


def validate_admission_runtimes(runtime_info: dict, *, compiler=_LOADED) -> None:
    """Refuse native admission code that implements a different rule revision:
    the compiler's contract admission and the VM's transaction admission."""
    if compiler is _LOADED:
        import xian_compiler_core as compiler
    revisions = compiler.compiler_version().get("admission_revisions") or {}
    if revisions.get(VM_EXECUTION_MODE) != COMPILER_ADMISSION_REVISION:
        raise ValueError(
            "native compiler admission revision does not match the node: "
            f"{revisions.get(VM_EXECUTION_MODE)!r} != {COMPILER_ADMISSION_REVISION!r}"
        )
    if runtime_info.get("tx_admission_revision") != TX_ADMISSION_REVISION:
        raise ValueError(
            "native transaction admission revision does not match the node: "
            f"{runtime_info.get('tx_admission_revision')!r} != {TX_ADMISSION_REVISION!r}"
        )


def vm_call_context(
    *,
    sender: str,
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    """The VM context of an external call from ``sender`` in ``environment``
    (the executor reads the owner itself)."""
    return {
        "signer": sender,
        "caller": sender,
        "this": contract_name,
        "entry": (contract_name, function_name),
        "submission_name": (kwargs.get("name") if contract_name == "submission" else None),
        "now": environment.get("now"),
        "block_num": environment.get("block_num"),
        "block_hash": environment.get("block_hash"),
        "chain_id": environment.get("chain_id"),
        "__input_hash": environment.get("__input_hash"),
    }
