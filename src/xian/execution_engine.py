from __future__ import annotations

import decimal
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from contracting.runtime_features import (
    RUNTIME_FEATURE_ZK,
    module_ir_uses_runtime_feature,
    runtime_feature_enabled,
)
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import validate_persistent_value

from xian.execution_policy import (
    EXECUTION_POLICY_KEY,
    SET_CODEC,
    VM_BYTECODE_VERSION,
    VM_EXECUTION_MODE,
    VM_EXECUTION_REVISION,
    VM_GAS_SCHEDULE,
)
from xian.utils.encoding import encode_abci_json, normalize_contract_result


@dataclass(frozen=True, slots=True)
class VmRuntime:
    runtime_info: dict[str, Any] | None = None

    @property
    def mode(self) -> str:
        return VM_EXECUTION_MODE


@dataclass(frozen=True, slots=True)
class VmPreparedContract:
    contract_name: str
    artifact_hash: str
    imported_contracts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VmExecutionResult:
    status_code: int
    result: Any
    writes: dict[str, Any]
    events: list[dict[str, Any]]
    chi_used: int = 0
    contract_costs: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class VmTransactionResult:
    output: VmExecutionResult
    writes: dict[str, Any]
    chi_used: int
    contract_costs: dict[str, int]
    reads: dict[str, Any]
    prefix_reads: frozenset[str]


class _InsufficientFeeBalance(AssertionError):
    pass


@dataclass(frozen=True, slots=True)
class _VmFeeReservation:
    balance_key: str
    amount: ContractingDecimal
    spendable_balance: object

    def refund_writes(self, writes: dict[str, Any], *, chi_used: int, chi_cost: int) -> dict:
        balance = _coerce_balance_value(writes.get(self.balance_key, self.spendable_balance))
        fee = ContractingDecimal(chi_used) / ContractingDecimal(chi_cost)
        # Subtract the actual charge from the reserved amount, rather than
        # independently rounding a second chi-to-token conversion for the refund.
        return {self.balance_key: balance + (self.amount - fee)}


_VM_PREPARED_CONTRACTS: dict[tuple[str, str], VmPreparedContract] = {}


def vm_requires_contract_source(
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
) -> bool:
    return (
        contract_name == "submission"
        and function_name == "submit_contract"
        and (not isinstance(kwargs.get("code"), str) or kwargs.get("code") == "")
    )


def vm_contract_source_error(
    contract_name: str,
    function_name: str,
) -> str:
    return f"xian_vm_v2 requires source code for {contract_name}.{function_name}"


def metering_write_keys(
    driver,
    *,
    sender: str,
    currency_contract: str = "currency",
    balances_hash: str = "balances",
) -> set[str]:
    if hasattr(driver, "make_key"):
        key = driver.make_key(currency_contract, balances_hash, [sender])
    else:
        key = f"{currency_contract}.{balances_hash}:{sender}"
    return {key}


def vm_metering_writes(
    driver,
    *,
    sender: str,
    chi_used: int,
    chi_cost: int,
    coerce_balance=None,
    base_writes: dict[str, Any] | None = None,
    currency_contract: str = "currency",
    balances_hash: str = "balances",
) -> dict[str, object]:
    if chi_used <= 0:
        return {}
    balances_key = next(
        iter(
            metering_write_keys(
                driver,
                sender=sender,
                currency_contract=currency_contract,
                balances_hash=balances_hash,
            )
        )
    )
    if coerce_balance is None:
        coerce_balance = _coerce_balance_value
    if base_writes is not None and balances_key in base_writes:
        balance = coerce_balance(base_writes[balances_key])
    else:
        driver_get = getattr(driver, "get", lambda _key: None)
        balance = coerce_balance(driver_get(balances_key))
    to_deduct = ContractingDecimal(chi_used) / ContractingDecimal(chi_cost)
    balance = max(balance - to_deduct, 0)
    return {balances_key: balance}


def _coerce_balance_value(balance):
    if isinstance(balance, ContractingDecimal):
        return balance
    if isinstance(balance, dict):
        return ContractingDecimal(balance.get("__fixed__"))
    if balance is None:
        return 0
    if isinstance(balance, str | float | decimal.Decimal):
        return ContractingDecimal(str(balance))
    return balance


def _reserve_vm_fee_budget(
    driver,
    *,
    sender: str,
    chi_budget: int,
    chi_cost: int,
    currency_contract: str,
    balances_hash: str,
) -> _VmFeeReservation:
    balance_key = next(
        iter(
            metering_write_keys(
                driver,
                sender=sender,
                currency_contract=currency_contract,
                balances_hash=balances_hash,
            )
        )
    )
    balance = _coerce_balance_value(driver.get(balance_key))
    # Compare exact ratios before fixed-point division truncates the reservation.
    balance_numerator, balance_denominator = decimal.Decimal(str(balance)).as_integer_ratio()
    rate_numerator, rate_denominator = decimal.Decimal(str(chi_cost)).as_integer_ratio()
    if chi_budget <= 0 or balance_numerator * rate_numerator < (
        chi_budget * balance_denominator * rate_denominator
    ):
        raise _InsufficientFeeBalance("Sender does not have enough chi for the submitted budget.")
    amount = ContractingDecimal(chi_budget) / ContractingDecimal(chi_cost)
    reservation = _VmFeeReservation(balance_key, amount, balance - amount)
    driver.apply_writes({balance_key: reservation.spendable_balance})
    return reservation


def clear_prepared_contract_cache() -> None:
    _VM_PREPARED_CONTRACTS.clear()


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

    return VmRuntime(runtime_info=runtime_info)


def prepare_vm_contract(
    runtime: VmRuntime,
    driver,
    contract_name: str,
) -> VmPreparedContract | None:
    return _prepare_vm_contract_bundle(
        driver=driver,
        contract_name=contract_name,
        stack=(),
    )


def execute_vm_contract(
    runtime: VmRuntime,
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
) -> VmExecutionResult:
    if vm_requires_contract_source(
        contract_name,
        function_name,
        kwargs,
    ):
        return VmExecutionResult(
            status_code=1,
            result=ValueError(vm_contract_source_error(contract_name, function_name)),
            writes={},
            events=[],
            chi_used=0,
            contract_costs={},
        )

    bindings = _load_vm_runtime_bindings()
    context = {
        "signer": sender,
        "caller": sender,
        "this": contract_name,
        "entry": (contract_name, function_name),
        "owner": getattr(driver, "get_owner", lambda _name: None)(contract_name),
        "submission_name": (kwargs.get("name") if contract_name == "submission" else None),
        "now": environment.get("now"),
        "block_num": environment.get("block_num"),
        "block_hash": environment.get("block_hash"),
        "chain_id": environment.get("chain_id"),
        "__input_hash": environment.get("__input_hash"),
    }
    try:
        output = bindings.execute_contract(
            vm_profile=VM_EXECUTION_MODE,
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
        return VmExecutionResult(
            status_code=1,
            result=exc,
            writes={},
            events=[],
            chi_used=0,
            contract_costs={},
        )

    return _validated_vm_result(
        VmExecutionResult(
            status_code=int(output.status_code),
            result=output.result,
            writes=dict(output.writes),
            events=list(output.events),
            chi_used=int(getattr(output, "chi_used", 0)),
            contract_costs=dict(getattr(output, "contract_costs", {}) or {}),
        )
    )


def _validated_vm_result(output: VmExecutionResult) -> VmExecutionResult:
    if output.status_code != 0:
        return output
    try:
        if EXECUTION_POLICY_KEY in output.writes:
            raise ValueError("contract effects cannot change the protected execution policy")
        validate_persistent_value((output.writes, output.events))
        normalize_contract_result(output.result)
        encode_abci_json((output.writes, output.events))
        for key in output.writes:
            key.encode("utf-8")
    except (ArithmeticError, AssertionError, TypeError, ValueError, RecursionError):
        return replace(
            output,
            status_code=1,
            result=ValueError("Contract writes or events cannot be encoded for consensus."),
            writes={},
            events=[],
        )
    return output


def execute_vm_transaction(
    runtime: VmRuntime,
    driver,
    *,
    sender: str,
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
    environment: dict[str, Any],
    chi_budget: int,
    chi_cost: int,
    meter: bool,
    transaction_size_bytes: int = 0,
    apply_metering_on_success_only: bool = True,
    apply_metering_writes: bool = True,
    reserve_fee_budget: bool = True,
    currency_contract: str = "currency",
    balances_hash: str = "balances",
) -> VmTransactionResult:
    base_driver_state = driver.snapshot_state()
    reservation = None
    try:
        if meter and apply_metering_writes and reserve_fee_budget:
            reservation = _reserve_vm_fee_budget(
                driver,
                sender=sender,
                chi_budget=chi_budget,
                chi_cost=chi_cost,
                currency_contract=currency_contract,
                balances_hash=balances_hash,
            )
        vm_output = execute_vm_contract(
            runtime,
            driver,
            sender=sender,
            contract_name=contract_name,
            function_name=function_name,
            kwargs=kwargs,
            environment=environment,
            meter=meter,
            chi_budget=chi_budget,
            transaction_size_bytes=transaction_size_bytes,
        )
    except _InsufficientFeeBalance as exc:
        vm_output = VmExecutionResult(
            status_code=1, result=AssertionError(str(exc)), writes={}, events=[]
        )
    finally:
        vm_reads = deepcopy(driver.transaction_reads)
        vm_prefix_reads = frozenset(getattr(driver, "transaction_read_prefixes", set()))
        driver.restore_state(base_driver_state)

    chi_used = int(vm_output.chi_used or 0)
    contract_costs = dict(vm_output.contract_costs or {})
    merged_writes = _settle_vm_transaction_writes(
        driver,
        vm_output,
        reservation=reservation,
        sender=sender,
        chi_cost=chi_cost,
        charge_fees=meter and apply_metering_writes,
        charge_failure=not apply_metering_on_success_only,
        currency_contract=currency_contract,
        balances_hash=balances_hash,
    )

    return VmTransactionResult(
        output=vm_output,
        writes=merged_writes,
        chi_used=chi_used,
        contract_costs=contract_costs,
        reads=vm_reads,
        prefix_reads=vm_prefix_reads,
    )


def _settle_vm_transaction_writes(
    driver,
    output: VmExecutionResult,
    *,
    reservation: _VmFeeReservation | None,
    sender: str,
    chi_cost: int,
    charge_fees: bool,
    charge_failure: bool,
    currency_contract: str,
    balances_hash: str,
) -> dict[str, Any]:
    writes = dict(output.writes) if output.status_code == 0 else {}
    if not charge_fees or (output.status_code != 0 and not charge_failure):
        return writes
    if reservation is not None:
        writes.update(
            reservation.refund_writes(writes, chi_used=output.chi_used, chi_cost=chi_cost)
        )
    else:
        writes.update(
            vm_metering_writes(
                driver,
                sender=sender,
                chi_used=output.chi_used,
                chi_cost=chi_cost,
                base_writes=writes,
                currency_contract=currency_contract,
                balances_hash=balances_hash,
            )
        )
    return writes


def _prepare_vm_contract_bundle(
    *,
    driver,
    contract_name: str,
    stack: tuple[str, ...],
) -> VmPreparedContract:
    if contract_name in stack:
        raise ValueError(
            "xian_vm_v2 import graph contains a cycle: " + " -> ".join((*stack, contract_name))
        )

    artifact_hash, module_ir_json = _load_vm_module_ir_json(driver, contract_name)
    module_ir = json.loads(module_ir_json)
    _reject_disabled_runtime_features(driver, contract_name, module_ir)

    cache_key = (contract_name, artifact_hash)
    cached = _VM_PREPARED_CONTRACTS.get(cache_key)
    if cached is not None:
        return cached

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
            stack=next_stack,
        )

    return prepared


def _reject_disabled_runtime_features(
    driver, contract_name: str, module_ir: dict[str, Any]
) -> None:
    if module_ir_uses_runtime_feature(
        module_ir,
        RUNTIME_FEATURE_ZK,
    ) and not runtime_feature_enabled(
        driver,
        RUNTIME_FEATURE_ZK,
        default_enabled=True,
    ):
        raise ValueError(
            f"contract '{contract_name}' uses zk host syscalls, "
            "but the chain zk runtime feature is disabled"
        )


def _load_vm_module_ir_json(
    driver,
    contract_name: str,
) -> tuple[str, str]:
    get_contract_ir = getattr(driver, "get_contract_ir", None)
    if get_contract_ir is not None:
        module_ir_json = get_contract_ir(
            contract_name,
            vm_profile=VM_EXECUTION_MODE,
        )
        if isinstance(module_ir_json, str) and module_ir_json:
            return (
                hashlib.sha256(module_ir_json.encode("utf-8")).hexdigest(),
                module_ir_json,
            )

    raise ValueError(
        "xian_vm_v2 requires persisted __xian_ir_v1__ for "
        f"contract '{contract_name}'; stored source is inspection-only"
    )
