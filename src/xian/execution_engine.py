from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from xian_runtime_types.encoding import safe_repr

from xian.execution_policy import ExecutionPolicy
from xian.utils.encoding import normalize_for_abci_json, stringify_decimals


@dataclass(frozen=True, slots=True)
class ExecutionRuntime:
    mode: str
    tracer_mode: str | None
    bytecode_version: str = ""
    gas_schedule: str = ""
    authority: str = ""
    shadow_tracer_mode: str = ""
    native_runtime_info: dict[str, Any] | None = None
    supports_transaction_execution: bool = True
    shadow_execution: bool = False
    native_authoritative: bool = False
    unavailable_reason: str = ""


@dataclass(frozen=True, slots=True)
class VmPreparedContract:
    contract_name: str
    artifact_hash: str
    imported_contracts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeExecutionResult:
    status_code: int
    result: Any
    writes: dict[str, Any]
    events: list[dict[str, Any]]
    chi_used: int = 0
    contract_costs: dict[str, int] | None = None


_VM_PREPARED_CONTRACTS: dict[
    tuple[str, str, str, str],
    VmPreparedContract,
] = {}


def native_execution_requires_deployment_artifacts(
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
) -> bool:
    return (
        contract_name == "submission"
        and function_name == "submit_contract"
        and not kwargs.get("deployment_artifacts")
    )


def clear_prepared_contract_cache() -> None:
    _VM_PREPARED_CONTRACTS.clear()


def snapshot_driver_state(driver) -> dict:
    return {
        "pending_writes": deepcopy(driver.pending_writes),
        "pending_reads": deepcopy(driver.pending_reads),
        "pending_deltas": deepcopy(driver.pending_deltas),
        "transaction_reads": deepcopy(driver.transaction_reads),
        "transaction_read_prefixes": deepcopy(
            getattr(driver, "transaction_read_prefixes", set())
        ),
        "transaction_writes": deepcopy(driver.transaction_writes),
        "log_events": deepcopy(driver.log_events),
    }


def restore_driver_state(driver, state_snapshot: dict | None) -> None:
    if not state_snapshot:
        return
    driver.pending_writes = deepcopy(state_snapshot["pending_writes"])
    driver.pending_reads = deepcopy(state_snapshot["pending_reads"])
    driver.pending_deltas = deepcopy(state_snapshot["pending_deltas"])
    driver.transaction_reads = deepcopy(state_snapshot["transaction_reads"])
    driver.transaction_read_prefixes = deepcopy(
        state_snapshot["transaction_read_prefixes"]
    )
    driver.transaction_writes = deepcopy(state_snapshot["transaction_writes"])
    driver.log_events = deepcopy(state_snapshot["log_events"])


@lru_cache(maxsize=1)
def _load_vm_runtime_bindings():
    try:
        import xian_vm_core
    except ImportError as exc:  # pragma: no cover - exercised via unit tests
        raise ValueError(
            "execution engine 'xian_vm_v1' requires the native "
            "'xian-tech-vm-core' package to be installed"
        ) from exc
    return xian_vm_core


def build_execution_runtime(policy: ExecutionPolicy) -> ExecutionRuntime:
    if policy.is_current_tracer_mode:
        return ExecutionRuntime(
            mode=policy.mode,
            tracer_mode=policy.mode,
            authority="python",
            supports_transaction_execution=True,
        )

    if policy.mode != "xian_vm_v1":
        raise ValueError(
            f"unsupported execution engine mode {policy.mode!r}"
        )

    bindings = _load_vm_runtime_bindings()
    runtime_info = bindings.runtime_info()
    if runtime_info.get("vm_profile") != policy.mode:
        raise ValueError(
            "xian_vm_v1 native runtime reported an unexpected vm_profile: "
            f"{runtime_info.get('vm_profile')!r}"
        )
    if not bindings.supports_execution_policy(
        policy.bytecode_version,
        policy.gas_schedule,
    ):
        raise ValueError(
            "xian_vm_v1 native runtime does not support execution policy "
            f"bytecode_version={policy.bytecode_version!r} "
            f"gas_schedule={policy.gas_schedule!r}"
        )

    authority = policy.authority or "python"
    if authority not in {"python", "native"}:
        raise ValueError(
            f"unsupported xian_vm_v1 authority {authority!r}"
        )
    if authority != "native" and not policy.shadow_tracer_mode:
        raise ValueError(
            "xian_vm_v1 runtime requires shadow_tracer_mode when authority "
            "is 'python'"
        )
    if authority == "native":
        return ExecutionRuntime(
            mode=policy.mode,
            tracer_mode=policy.shadow_tracer_mode or None,
            bytecode_version=policy.bytecode_version,
            gas_schedule=policy.gas_schedule,
            authority="native",
            shadow_tracer_mode=policy.shadow_tracer_mode,
            native_runtime_info=runtime_info,
            supports_transaction_execution=True,
            shadow_execution=bool(policy.shadow_tracer_mode),
            native_authoritative=True,
            unavailable_reason="",
        )

    return ExecutionRuntime(
        mode=policy.mode,
        tracer_mode=policy.shadow_tracer_mode or None,
        bytecode_version=policy.bytecode_version,
        gas_schedule=policy.gas_schedule,
        authority="python",
        shadow_tracer_mode=policy.shadow_tracer_mode,
        native_runtime_info=runtime_info,
        supports_transaction_execution=True,
        shadow_execution=True,
        native_authoritative=False,
        unavailable_reason="",
    )


def prepare_contract_for_execution(
    runtime: ExecutionRuntime,
    driver,
    contract_name: str,
) -> VmPreparedContract | None:
    if runtime.mode != "xian_vm_v1":
        return None

    return _prepare_vm_contract_bundle(
        driver=driver,
        contract_name=contract_name,
        bytecode_version=runtime.bytecode_version,
        gas_schedule=runtime.gas_schedule,
        stack=(),
    )


def execute_native_contract(
    runtime: ExecutionRuntime,
    driver,
    *,
    sender: str,
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
    environment: dict[str, Any],
    meter: bool = False,
    chi_budget: int = 0,
    transaction_size_bytes: int = 0,
) -> NativeExecutionResult:
    if runtime.mode != "xian_vm_v1":
        raise ValueError(
            "execute_native_contract() requires an xian_vm_v1 runtime"
        )

    bindings = _load_vm_runtime_bindings()
    context = {
        "signer": sender,
        "caller": sender,
        "this": contract_name,
        "entry": (contract_name, function_name),
        "owner": driver.get_owner(contract_name),
        "submission_name": (
            kwargs.get("name") if contract_name == "submission" else None
        ),
        "now": environment.get("now"),
        "block_num": environment.get("block_num"),
        "block_hash": environment.get("block_hash"),
        "chain_id": environment.get("chain_id"),
    }
    try:
        output = bindings.execute_contract(
            driver=driver,
            contract_name=contract_name,
            function_name=function_name,
            args=[],
            kwargs=kwargs,
            context=context,
            meter=meter,
            chi_budget_raw=max(int(chi_budget), 0) * 1000,
            transaction_size_bytes=max(int(transaction_size_bytes), 0),
        )
    except Exception as exc:
        return NativeExecutionResult(
            status_code=1,
            result=exc,
            writes={},
            events=[],
            chi_used=0,
            contract_costs={},
        )

    return NativeExecutionResult(
        status_code=int(output.status_code),
        result=output.result,
        writes=dict(output.writes),
        events=list(output.events),
        chi_used=int(getattr(output, "chi_used", 0)),
        contract_costs=dict(getattr(output, "contract_costs", {}) or {}),
    )


def compare_execution_results(
    authoritative_output: dict[str, Any],
    native_output: NativeExecutionResult,
    *,
    ignore_write_keys: set[str] | None = None,
) -> dict[str, tuple[Any, Any]]:
    normalized_authoritative = _normalize_execution_output(
        authoritative_output["status_code"],
        authoritative_output["result"],
        authoritative_output.get("writes", {}),
        authoritative_output.get("events", []),
        ignore_write_keys=ignore_write_keys,
    )
    normalized_native = _normalize_execution_output(
        native_output.status_code,
        native_output.result,
        native_output.writes,
        native_output.events,
        ignore_write_keys=ignore_write_keys,
    )

    mismatches = {}
    for field in ("status_code", "result", "writes", "events"):
        if normalized_authoritative[field] != normalized_native[field]:
            mismatches[field] = (
                normalized_authoritative[field],
                normalized_native[field],
            )
    return mismatches


def _normalize_execution_output(
    status_code: int,
    result: Any,
    writes: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    ignore_write_keys: set[str] | None = None,
) -> dict[str, Any]:
    ignored = ignore_write_keys or set()
    normalized_writes = {
        key: _normalize_shadow_value(value)
        for key, value in sorted(writes.items())
        if key not in ignored
    }
    return {
        "status_code": status_code,
        "result": _normalize_shadow_value(result),
        "writes": normalized_writes,
        "events": _normalize_shadow_value(events),
    }


def _normalize_shadow_value(value: Any) -> Any:
    if isinstance(value, BaseException):
        return safe_repr(value)
    return stringify_decimals(normalize_for_abci_json(value))


def _prepare_vm_contract_bundle(
    *,
    driver,
    contract_name: str,
    bytecode_version: str,
    gas_schedule: str,
    stack: tuple[str, ...],
) -> VmPreparedContract:
    if contract_name in stack:
        raise ValueError(
            "xian_vm_v1 import graph contains a cycle: "
            + " -> ".join((*stack, contract_name))
        )

    artifact_hash, module_ir_json = _load_vm_module_ir_json(driver, contract_name)
    cache_key = (
        contract_name,
        artifact_hash,
        bytecode_version,
        gas_schedule,
    )
    cached = _VM_PREPARED_CONTRACTS.get(cache_key)
    if cached is not None:
        return cached

    module_ir = json.loads(module_ir_json)
    bindings = _load_vm_runtime_bindings()
    bindings.validate_module_ir(module_ir)

    imported_contracts = tuple(
        sorted(
            {
                str(import_spec["module"])
                for import_spec in module_ir.get("imports", [])
                if import_spec.get("module")
            }
        )
    )
    prepared = VmPreparedContract(
        contract_name=contract_name,
        artifact_hash=artifact_hash,
        imported_contracts=imported_contracts,
    )
    _VM_PREPARED_CONTRACTS[cache_key] = prepared

    next_stack = (*stack, contract_name)
    for imported_contract in imported_contracts:
        _prepare_vm_contract_bundle(
            driver=driver,
            contract_name=imported_contract,
            bytecode_version=bytecode_version,
            gas_schedule=gas_schedule,
            stack=next_stack,
        )

    return prepared


def _load_vm_module_ir_json(
    driver,
    contract_name: str,
) -> tuple[str, str]:
    get_contract_ir = getattr(driver, "get_contract_ir", None)
    if get_contract_ir is not None:
        module_ir_json = get_contract_ir(
            contract_name,
            vm_profile="xian_vm_v1",
        )
        if isinstance(module_ir_json, str) and module_ir_json:
            return (
                hashlib.sha256(module_ir_json.encode("utf-8")).hexdigest(),
                module_ir_json,
            )

    raise ValueError(
        "xian_vm_v1 requires persisted __xian_ir_v1__ for "
        f"contract '{contract_name}'; stored source is inspection-only"
    )
