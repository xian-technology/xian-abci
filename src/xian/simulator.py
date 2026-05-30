from __future__ import annotations

import asyncio
import hashlib
import json
import pickle
import sys
from pathlib import Path

from contracting.local import ContractingClient
from loguru import logger
from xian_runtime_types.encoding import convert_dict
from xian_runtime_types.time import Datetime

from xian.app_logging import build_log_fields
from xian.execution_engine import (
    VmRuntime,
    execute_vm_transaction,
    prepare_vm_contract,
    restore_driver_state,
    snapshot_driver_state,
    vm_deployment_artifacts_error,
    vm_requires_deployment_artifacts,
)
from xian.utils.block import (
    get_latest_block_nanos,
    nanoseconds_to_utc_datetime,
)
from xian.utils.encoding import normalize_for_abci_json, stringify_decimals
from xian.utils.tx import format_dictionary


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
        execution_runtime: VmRuntime | None = None,
        chain_id: str | None = None,
    ):
        self.client = client
        self.get_block_meta = get_block_meta or (lambda: None)
        self.execution_runtime = execution_runtime or VmRuntime()
        self.chain_id = chain_id

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
            VmRuntime(),
        )
        state_snapshot = self._snapshot_driver_state()
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
            prepare_vm_contract(
                execution_runtime,
                self.client.raw_driver,
                payload["contract"],
            )
            converted_kwargs = convert_dict(payload.get("kwargs", {}))
            if vm_requires_deployment_artifacts(
                payload["contract"],
                payload["function"],
                converted_kwargs,
            ):
                return _simulation_error_result(
                    payload=payload,
                    message=vm_deployment_artifacts_error(
                        payload["contract"],
                        payload["function"],
                    ),
                )
            environment = self._make_environment(payload, block_meta=block_meta)
            return self._execute_vm_simulation(
                payload=payload,
                kwargs=converted_kwargs,
                environment=environment,
                max_chi=max_chi,
                chi_cost=chi_cost,
            )
        finally:
            self._restore_driver_state(state_snapshot)

    def _snapshot_driver_state(self) -> dict:
        return snapshot_driver_state(self.client.raw_driver)

    def _restore_driver_state(self, state_snapshot: dict) -> None:
        restore_driver_state(self.client.raw_driver, state_snapshot)

    def _execute_vm_simulation(
        self,
        *,
        payload: dict,
        kwargs: dict,
        environment: dict,
        max_chi: int | None,
        chi_cost: int,
    ) -> dict:
        outcome = execute_vm_transaction(
            self.execution_runtime,
            self.client.raw_driver,
            sender=payload["sender"],
            contract_name=payload["contract"],
            function_name=payload["function"],
            kwargs=kwargs,
            environment=environment,
            chi_budget=max(int(max_chi or 1_000_000), 1),
            chi_cost=chi_cost,
            meter=True,
            apply_metering_on_success_only=False,
        )

        writes = [{"key": key, "value": value} for key, value in outcome.writes.items()]
        result = {
            "payload": payload,
            "status": outcome.output.status_code,
            "state": writes,
            "chi_used": outcome.chi_used,
            "result": normalize_for_abci_json(outcome.output.result),
        }
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
        block_meta = block_meta or ((self.get_block_meta() or {}) if self.get_block_meta else {})
        block_nanos = block_meta.get("nanos")
        if block_nanos is None:
            block_nanos = get_latest_block_nanos(self.client.raw_driver.storage_home)
        payload_hash = self._payload_hash(payload or {})
        chain_id = block_meta.get("chain_id") or self.chain_id
        block_hash = block_meta.get("hash")
        if block_hash is None:
            block_hash = hashlib.sha3_256(
                (
                    f"simulate:block:{chain_id or ''}:"
                    f"{block_meta.get('height', block_num)}:{int(block_nanos or 0)}"
                ).encode("utf-8")
            ).hexdigest()
        input_hash = hashlib.sha3_256(
            f"{int(block_nanos or 0)}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        now = Datetime._from_datetime(nanoseconds_to_utc_datetime(int(block_nanos or 0)))
        execution_runtime = getattr(self, "execution_runtime", None)
        return {
            "block_hash": block_hash,
            "block_num": block_meta.get("height", block_num),
            "__input_hash": input_hash,
            "__xian_execution_mode__": (getattr(execution_runtime, "mode", None) or "xian_vm_v1"),
            "now": now,
            "chain_id": chain_id,
        }


class QuerySimulationService:
    def __init__(
        self,
        *,
        storage_home: str | Path,
        execution_runtime: VmRuntime | None = None,
        get_block_meta=None,
        get_state_snapshot=None,
        chain_id: str | None = None,
        enabled: bool = True,
        max_concurrency: int = 2,
        timeout_ms: int = 3000,
        max_chi: int = 1_000_000,
    ) -> None:
        self.storage_home = Path(storage_home)
        self.execution_runtime = execution_runtime or VmRuntime()
        self.get_block_meta = get_block_meta or (lambda: None)
        self.get_state_snapshot = get_state_snapshot or (lambda: None)
        self.chain_id = chain_id
        self.enabled = enabled
        self.max_concurrency = max(int(max_concurrency), 1)
        self.timeout_ms = max(int(timeout_ms), 1)
        self.max_chi = max(int(max_chi), 1)
        self._active_requests = 0
        self._counter_lock = asyncio.Lock()

    async def simulate_encoded_transaction(self, raw_payload_hex: str) -> dict:
        try:
            decoded_payload = json.loads(bytes.fromhex(raw_payload_hex).decode("utf-8"))
            normalized_payload = TransactionSimulator._normalize_payload(decoded_payload)
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
                            "simulation_max_concurrency": (self.max_concurrency),
                        },
                    )
                ).warning("Rejected readonly simulation because capacity is exhausted")
                return _simulation_error_result(
                    payload=normalized_payload,
                    message=("Simulation capacity exceeded on this node; retry later"),
                )
            self._active_requests += 1

        try:
            task = {
                "storage_home": str(self.storage_home),
                "execution_runtime": self.execution_runtime,
                "payload": normalized_payload,
                "block_meta": self.get_block_meta() or {},
                "chain_id": self.chain_id,
                "max_chi": self.max_chi,
                "driver_state": self.get_state_snapshot(),
            }
            result = await self._run_task(task, normalized_payload)
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
                message=(f"Simulation timed out on this node after {self.timeout_ms} ms"),
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
                detail = f"Simulation worker exited with code {process.returncode}"
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
