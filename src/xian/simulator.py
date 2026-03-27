from __future__ import annotations

import asyncio
import hashlib
import json
import pickle
import sys
from copy import deepcopy
from pathlib import Path

from contracting.client import ContractingClient
from contracting.execution.executor import Executor
from loguru import logger
from xian_runtime_types.encoding import convert_dict, safe_repr
from xian_runtime_types.time import Datetime

from xian.app_logging import build_log_fields
from xian.utils.block import (
    get_latest_block_nanos,
    nanoseconds_to_utc_datetime,
)
from xian.utils.encoding import stringify_decimals
from xian.utils.tx import format_dictionary


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


def _simulation_error_result(
    *,
    payload: dict | None,
    message: str,
) -> dict:
    return {
        "payload": payload,
        "status": 1,
        "state": [],
        "stamps_used": 0,
        "result": message,
    }


class TransactionSimulator:
    def __init__(
        self,
        *,
        client: ContractingClient,
        get_block_meta=None,
    ):
        self.client = client
        self.get_block_meta = get_block_meta or (lambda: None)
        self.executor = Executor(
            driver=self.client.raw_driver,
            metering=False,
            bypass_balance_amount=True,
        )

    def simulate(
        self,
        payload: dict,
        *,
        block_meta: dict | None = None,
        max_stamps: int | None = None,
    ) -> dict:
        normalized_payload = self._normalize_payload(payload)
        try:
            return self._execute(
                normalized_payload,
                block_meta=block_meta,
                max_stamps=max_stamps,
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
        max_stamps: int | None = None,
    ) -> dict:
        decoded = json.loads(bytes.fromhex(raw_payload_hex).decode("utf-8"))
        return self.simulate(
            decoded,
            block_meta=block_meta,
            max_stamps=max_stamps,
        )

    def _execute(
        self,
        payload: dict,
        *,
        block_meta: dict | None = None,
        max_stamps: int | None = None,
    ) -> dict:
        state_snapshot = self._snapshot_driver_state()
        try:
            stamp_cost = int(
                self.client.get_var(
                    contract="stamp_cost",
                    variable="S",
                    arguments=["value"],
                )
            )
        except Exception:
            stamp_cost = 20

        try:
            output = self.executor.execute(
                sender=payload["sender"],
                contract_name=payload["contract"],
                function_name=payload["function"],
                stamps=max(int(max_stamps or 1_000_000), 1),
                stamp_cost=stamp_cost,
                kwargs=convert_dict(payload.get("kwargs", {})),
                environment=self._make_environment(
                    payload, block_meta=block_meta
                ),
                auto_commit=False,
                metering=True,
            )

            writes = [
                {"key": key, "value": value}
                for key, value in output["writes"].items()
            ]

            result = {
                "payload": payload,
                "status": output["status_code"],
                "state": writes,
                "stamps_used": output["stamps_used"],
                "result": safe_repr(output["result"]),
            }
            return stringify_decimals(format_dictionary(result))
        finally:
            self._restore_driver_state(state_snapshot)

    def _snapshot_driver_state(self) -> dict:
        return snapshot_driver_state(self.client.raw_driver)

    def _restore_driver_state(self, state_snapshot: dict) -> None:
        restore_driver_state(self.client.raw_driver, state_snapshot)

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
        return {
            "block_hash": block_hash,
            "block_num": block_meta.get("height", block_num),
            "__input_hash": input_hash,
            "now": now,
            "chain_id": block_meta.get("chain_id"),
        }


class QuerySimulationService:
    def __init__(
        self,
        *,
        storage_home: str | Path,
        tracer_mode: str,
        get_block_meta=None,
        get_state_snapshot=None,
        enabled: bool = True,
        max_concurrency: int = 2,
        timeout_ms: int = 3000,
        max_stamps: int = 1_000_000,
    ) -> None:
        self.storage_home = Path(storage_home)
        self.tracer_mode = tracer_mode
        self.get_block_meta = get_block_meta or (lambda: None)
        self.get_state_snapshot = get_state_snapshot or (lambda: None)
        self.enabled = enabled
        self.max_concurrency = max(int(max_concurrency), 1)
        self.timeout_ms = max(int(timeout_ms), 1)
        self.max_stamps = max(int(max_stamps), 1)
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
                "payload": normalized_payload,
                "block_meta": self.get_block_meta() or {},
                "max_stamps": self.max_stamps,
                "driver_state": self.get_state_snapshot(),
            }
            return await self._run_task(task, normalized_payload)
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
