from __future__ import annotations

import asyncio
import hashlib
import json
import pickle
import sys
from pathlib import Path

from contracting.client import ContractingClient
from contracting.execution.executor import Executor
from loguru import logger
from xian_runtime_types.encoding import convert_dict
from xian_runtime_types.time import Datetime

from xian.app_logging import build_log_fields
from xian.execution_engine import (
    ExecutionRuntime,
    augment_execution_output_with_driver_state,
    compare_execution_results,
    execute_authoritative_native_contract,
    execute_native_contract,
    metering_write_keys,
    prepare_contract_for_execution,
    restore_driver_state,
    snapshot_driver_state,
    xian_vm_deployment_artifacts_error,
    xian_vm_requires_deployment_artifacts,
)
from xian.utils.block import (
    get_latest_block_nanos,
    nanoseconds_to_utc_datetime,
)
from xian.utils.encoding import normalize_for_abci_json, stringify_decimals
from xian.utils.tx import format_dictionary

_SHADOW_COMPARISONS_KEY = "__xian_shadow_comparisons__"


def _simulation_error_result(
    *,
    payload: dict | None,
    message: str,
) -> dict:
    return {
        "payload": payload,
        "status": 1,
        "state": [],
        "chi_used": 0,
        "result": message,
    }


class TransactionSimulator:
    def __init__(
        self,
        *,
        client: ContractingClient,
        get_block_meta=None,
        execution_runtime: ExecutionRuntime | None = None,
        shadow_observer=None,
        collect_shadow_comparisons: bool = False,
    ):
        self.client = client
        self.get_block_meta = get_block_meta or (lambda: None)
        self.execution_runtime = execution_runtime or ExecutionRuntime(
            mode="python_line_v1",
            tracer_mode="python_line_v1",
        )
        self.executor = Executor(
            driver=self.client.raw_driver,
            metering=False,
            bypass_balance_amount=True,
        )
        self.shadow_observer = shadow_observer
        self.collect_shadow_comparisons = collect_shadow_comparisons

    def simulate(
        self,
        payload: dict,
        *,
        block_meta: dict | None = None,
        max_chi: int | None = None,
    ) -> dict:
        normalized_payload = self._normalize_payload(payload)
        try:
            return self._execute(
                normalized_payload,
                block_meta=block_meta,
                max_chi=max_chi,
            )
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=normalized_payload,
                    extra={"error_type": type(exc).__name__},
                )
            ).error("Simulation failed: {}", exc)
            return _simulation_error_result(
                payload=normalized_payload,
                message=f"Simulation error: {exc}",
            )

    def simulate_encoded_transaction(
        self,
        raw_payload_hex: str,
        *,
        block_meta: dict | None = None,
        max_chi: int | None = None,
    ) -> dict:
        decoded = json.loads(bytes.fromhex(raw_payload_hex).decode("utf-8"))
        return self.simulate(
            decoded,
            block_meta=block_meta,
            max_chi=max_chi,
        )

    def _execute(
        self,
        payload: dict,
        *,
        block_meta: dict | None = None,
        max_chi: int | None = None,
    ) -> dict:
        execution_runtime = getattr(
            self,
            "execution_runtime",
            ExecutionRuntime(
                mode="python_line_v1",
                tracer_mode="python_line_v1",
            ),
        )
        state_snapshot = self._snapshot_driver_state()
        shadow_comparisons = []
        try:
            chi_cost = int(
                self.client.get_var(
                    contract="chi_cost",
                    variable="S",
                    arguments=["value"],
                )
            )
        except Exception:
            chi_cost = 20

        try:
            prepare_contract_for_execution(
                execution_runtime,
                self.client.raw_driver,
                payload["contract"],
            )
            converted_kwargs = convert_dict(payload.get("kwargs", {}))
            if xian_vm_requires_deployment_artifacts(
                execution_runtime,
                payload["contract"],
                payload["function"],
                converted_kwargs,
            ):
                return _simulation_error_result(
                    payload=payload,
                    message=xian_vm_deployment_artifacts_error(
                        payload["contract"],
                        payload["function"],
                    ),
                )
            environment = self._make_environment(payload, block_meta=block_meta)
            if getattr(execution_runtime, "native_authoritative", False):
                return self._execute_native_authoritative_simulation(
                    payload=payload,
                    kwargs=converted_kwargs,
                    environment=environment,
                    max_chi=max_chi,
                    chi_cost=chi_cost,
                )
            track_driver_output = getattr(
                execution_runtime, "shadow_execution", False
            ) or (
                payload["contract"] == "submission"
                and payload["function"] == "submit_contract"
            )
            output = self.executor.execute(
                sender=payload["sender"],
                contract_name=payload["contract"],
                function_name=payload["function"],
                chi=max(int(max_chi or 1_000_000), 1),
                chi_cost=chi_cost,
                kwargs=converted_kwargs,
                environment=environment,
                auto_commit=False,
                metering=True,
            )
            if track_driver_output:
                output = augment_execution_output_with_driver_state(
                    output,
                    before_state=state_snapshot,
                    after_state=snapshot_driver_state(self.client.raw_driver),
                )
            if getattr(execution_runtime, "shadow_execution", False):
                shadow_comparisons.append(
                    self._run_native_shadow_execution(
                        payload=payload,
                        kwargs=converted_kwargs,
                        output=output,
                        environment=environment,
                        base_driver_state=state_snapshot,
                    )
                )

            writes = [
                {"key": key, "value": value}
                for key, value in output["writes"].items()
            ]

            result = {
                "payload": payload,
                "status": output["status_code"],
                "state": writes,
                "chi_used": output["chi_used"],
                "result": normalize_for_abci_json(output["result"]),
            }
            if getattr(self, "collect_shadow_comparisons", False):
                result[_SHADOW_COMPARISONS_KEY] = shadow_comparisons
            return stringify_decimals(format_dictionary(result))
        finally:
            self._restore_driver_state(state_snapshot)

    def _snapshot_driver_state(self) -> dict:
        return snapshot_driver_state(self.client.raw_driver)

    def _restore_driver_state(self, state_snapshot: dict) -> None:
        restore_driver_state(self.client.raw_driver, state_snapshot)

    def _run_native_shadow_execution(
        self,
        *,
        payload: dict,
        kwargs: dict,
        output: dict,
        environment: dict,
        base_driver_state: dict,
    ) -> None:
        restore_driver_state(self.client.raw_driver, base_driver_state)
        native_output = execute_native_contract(
            self.execution_runtime,
            self.client.raw_driver,
            sender=payload["sender"],
            contract_name=payload["contract"],
            function_name=payload["function"],
            kwargs=kwargs,
            environment=environment,
            meter=True,
            chi_budget=1_000_000,
        )
        output_for_compare = augment_execution_output_with_driver_state(
            output,
            before_state=base_driver_state,
            after_state=snapshot_driver_state(self.client.raw_driver),
        )
        mismatches = compare_execution_results(
            output_for_compare,
            native_output,
            ignore_write_keys=metering_write_keys(
                self.client.raw_driver,
                sender=payload["sender"],
                currency_contract=getattr(
                    self.executor, "currency_contract", "currency"
                ),
                balances_hash=getattr(
                    self.executor, "balances_hash", "balances"
                ),
            ),
        )
        shadow_observer = getattr(self, "shadow_observer", None)
        if shadow_observer is not None:
            shadow_observer.record_comparison(
                stage="simulate_tx_native_shadow",
                contract=payload["contract"],
                function=payload["function"],
                sender=payload.get("sender"),
                nonce=payload.get("nonce"),
                block_height=environment.get("block_num"),
                mismatches=mismatches,
            )
        if mismatches:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx_native_shadow",
                    payload=payload,
                    extra={"mismatch_fields": sorted(mismatches)},
                )
            ).warning(
                "Native VM simulation shadow mismatch: {}",
                mismatches,
            )
        return {
            "stage": "simulate_tx_native_shadow",
            "contract": payload["contract"],
            "function": payload["function"],
            "sender": payload.get("sender"),
            "nonce": payload.get("nonce"),
            "block_height": environment.get("block_num"),
            "mismatches": mismatches,
        }

    def _execute_native_authoritative_simulation(
        self,
        *,
        payload: dict,
        kwargs: dict,
        environment: dict,
        max_chi: int | None,
        chi_cost: int,
    ) -> dict:
        outcome = execute_authoritative_native_contract(
            self.execution_runtime,
            self.client.raw_driver,
            executor=self.executor,
            sender=payload["sender"],
            contract_name=payload["contract"],
            function_name=payload["function"],
            kwargs=kwargs,
            environment=environment,
            chi_budget=max(int(max_chi or 1_000_000), 1),
            chi_cost=chi_cost,
            meter=True,
            mismatch_label="native authoritative simulation",
            apply_metering_on_success_only=False,
            shadow_observer=getattr(self, "shadow_observer", None),
            shadow_stage="simulate_tx_native_authoritative",
            shadow_context={
                "contract": payload["contract"],
                "function": payload["function"],
                "sender": payload.get("sender"),
                "nonce": payload.get("nonce"),
                "block_height": environment.get("block_num"),
            },
        )

        writes = [
            {"key": key, "value": value}
            for key, value in outcome.writes.items()
        ]
        result = {
            "payload": payload,
            "status": outcome.output.status_code,
            "state": writes,
            "chi_used": outcome.chi_used,
            "result": normalize_for_abci_json(outcome.output.result),
        }
        if getattr(self, "collect_shadow_comparisons", False):
            result[_SHADOW_COMPARISONS_KEY] = [
                {
                    "stage": "simulate_tx_native_authoritative",
                    "contract": payload["contract"],
                    "function": payload["function"],
                    "sender": payload.get("sender"),
                    "nonce": payload.get("nonce"),
                    "block_height": environment.get("block_num"),
                    "mismatches": outcome.shadow_mismatches,
                }
            ]
        return stringify_decimals(format_dictionary(result))

    @staticmethod
    def _normalize_payload(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("simulation payload must be a JSON object")

        normalized = payload.get("payload", payload)
        if not isinstance(normalized, dict):
            raise ValueError("simulation payload must resolve to a JSON object")

        kwargs = normalized.get("kwargs", {})
        if kwargs is None:
            kwargs = {}
        if not isinstance(kwargs, dict):
            raise ValueError("simulation payload kwargs must be a JSON object")

        for field in ("sender", "contract", "function"):
            value = normalized.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"simulation payload missing {field}")

        return {
            "sender": normalized["sender"],
            "contract": normalized["contract"],
            "function": normalized["function"],
            "kwargs": kwargs,
        }

    @staticmethod
    def _payload_hash(payload: dict) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha3_256(encoded).hexdigest()

    def _make_environment(
        self,
        payload: dict | None = None,
        block_num: int = 1,
        *,
        block_meta: dict | None = None,
    ) -> dict:
        block_meta = block_meta or (
            (self.get_block_meta() or {}) if self.get_block_meta else {}
        )
        block_nanos = block_meta.get("nanos")
        if block_nanos is None:
            block_nanos = get_latest_block_nanos(
                self.client.raw_driver.storage_home
            )
        payload_hash = self._payload_hash(payload or {})
        block_hash = block_meta.get("hash")
        if block_hash is None:
            block_hash = hashlib.sha3_256(
                (
                    f"simulate:block:{block_meta.get('chain_id') or ''}:"
                    f"{block_meta.get('height', block_num)}:{int(block_nanos or 0)}"
                ).encode("utf-8")
            ).hexdigest()
        input_hash = hashlib.sha3_256(
            f"{int(block_nanos or 0)}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        now = Datetime._from_datetime(
            nanoseconds_to_utc_datetime(int(block_nanos or 0))
        )
        execution_runtime = getattr(self, "execution_runtime", None)
        return {
            "block_hash": block_hash,
            "block_num": block_meta.get("height", block_num),
            "__input_hash": input_hash,
            "__xian_execution_mode__": (
                getattr(execution_runtime, "mode", None) or "python_line_v1"
            ),
            "now": now,
            "chain_id": block_meta.get("chain_id"),
        }


class QuerySimulationService:
    def __init__(
        self,
        *,
        storage_home: str | Path,
        tracer_mode: str,
        execution_runtime: ExecutionRuntime | None = None,
        get_block_meta=None,
        get_state_snapshot=None,
        enabled: bool = True,
        max_concurrency: int = 2,
        timeout_ms: int = 3000,
        max_chi: int = 1_000_000,
        shadow_observer=None,
    ) -> None:
        self.storage_home = Path(storage_home)
        self.tracer_mode = tracer_mode
        self.execution_runtime = execution_runtime or ExecutionRuntime(
            mode=tracer_mode,
            tracer_mode=tracer_mode,
        )
        self.get_block_meta = get_block_meta or (lambda: None)
        self.get_state_snapshot = get_state_snapshot or (lambda: None)
        self.enabled = enabled
        self.max_concurrency = max(int(max_concurrency), 1)
        self.timeout_ms = max(int(timeout_ms), 1)
        self.max_chi = max(int(max_chi), 1)
        self.shadow_observer = shadow_observer
        self._active_requests = 0
        self._counter_lock = asyncio.Lock()

    async def simulate_encoded_transaction(self, raw_payload_hex: str) -> dict:
        try:
            decoded_payload = json.loads(
                bytes.fromhex(raw_payload_hex).decode("utf-8")
            )
            normalized_payload = TransactionSimulator._normalize_payload(
                decoded_payload
            )
        except Exception as exc:
            return _simulation_error_result(
                payload=None,
                message=f"Simulation error: {exc}",
            )

        if not self.enabled:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=normalized_payload,
                )
            ).info("Rejected readonly simulation because it is disabled")
            return _simulation_error_result(
                payload=normalized_payload,
                message="Simulation is disabled on this node",
            )

        async with self._counter_lock:
            if self._active_requests >= self.max_concurrency:
                logger.bind(
                    **build_log_fields(
                        stage="simulate_tx",
                        payload=normalized_payload,
                        extra={
                            "active_requests": self._active_requests,
                            "simulation_max_concurrency": (
                                self.max_concurrency
                            ),
                        },
                    )
                ).warning(
                    "Rejected readonly simulation because capacity is exhausted"
                )
                return _simulation_error_result(
                    payload=normalized_payload,
                    message=(
                        "Simulation capacity exceeded on this node; retry later"
                    ),
                )
            self._active_requests += 1

        try:
            task = {
                "storage_home": str(self.storage_home),
                "tracer_mode": self.tracer_mode,
                "execution_runtime": self.execution_runtime,
                "payload": normalized_payload,
                "block_meta": self.get_block_meta() or {},
                "max_chi": self.max_chi,
                "driver_state": self.get_state_snapshot(),
            }
            result = await self._run_task(task, normalized_payload)
            self._record_shadow_comparisons(result)
            return result
        finally:
            async with self._counter_lock:
                self._active_requests -= 1

    async def _run_task(self, task: dict, payload: dict) -> dict:
        process = None
        try:
            process = await self._start_task_process(task)
            return await asyncio.wait_for(
                self._wait_for_task_result(process, task),
                timeout=self.timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            await self._terminate_process(process)
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=payload,
                    extra={"simulation_timeout_ms": self.timeout_ms},
                )
            ).warning("Readonly simulation timed out")
            return _simulation_error_result(
                payload=payload,
                message=(
                    "Simulation timed out on this node after "
                    f"{self.timeout_ms} ms"
                ),
            )
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=payload,
                    extra={"error_type": type(exc).__name__},
                )
            ).error("Simulation worker failed: {}", exc)
            return _simulation_error_result(
                payload=payload,
                message=f"Simulation error: {exc}",
            )
        finally:
            if process is not None:
                await self._terminate_process(process)

    async def _start_task_process(self, task: dict):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "xian.simulator_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _wait_for_task_result(self, process, task: dict) -> dict:
        stdout, stderr = await process.communicate(pickle.dumps(task))
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = (
                    f"Simulation worker exited with code {process.returncode}"
                )
            raise RuntimeError(detail)

        try:
            return pickle.loads(stdout)
        except Exception as exc:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            detail = stderr_text or "Simulation worker returned invalid data"
            raise RuntimeError(f"{detail}: {exc}") from exc

    @staticmethod
    async def _terminate_process(process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.1)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    def close(self) -> None:
        return None

    def _record_shadow_comparisons(self, result: dict) -> None:
        comparisons = result.pop(_SHADOW_COMPARISONS_KEY, None)
        observer = getattr(self, "shadow_observer", None)
        if observer is None or not comparisons:
            return
        for comparison in comparisons:
            observer.record_comparison(
                stage=comparison.get("stage", "simulate_tx"),
                contract=comparison.get("contract"),
                function=comparison.get("function"),
                sender=comparison.get("sender"),
                nonce=comparison.get("nonce"),
                block_height=comparison.get("block_height"),
                mismatches=comparison.get("mismatches") or {},
            )
