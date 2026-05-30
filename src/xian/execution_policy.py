from __future__ import annotations

VM_EXECUTION_MODE = "xian_vm_v1"
VM_BYTECODE_VERSION = "xvm-1"
VM_GAS_SCHEDULE = "xvm-gas-1"


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
