from __future__ import annotations

import multiprocessing
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from contracting.execution.parallel import (
    ExecutionAccess,
    SpeculativeExecutionController,
)
from contracting.local import ContractingClient
from contracting.storage.driver import Driver
from loguru import logger

from xian.execution_engine import VmRuntime
from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler


@dataclass
class _WorkerRuntime:
    client: ContractingClient
    tx_processor: TxProcessor
    rewards_handler: RewardsHandler | None


_WORKER_RUNTIMES: dict[tuple[object, ...], _WorkerRuntime] = {}


@dataclass(frozen=True)
class ParallelExecutionStats:
    worker_count: int
    estimated_known_transactions: int
    estimated_unknown_transactions: int
    estimated_stage_count: int
    estimated_parallelizable_transactions: int
    estimated_known_shapes: tuple[dict[str, object], ...]
    estimated_unknown_shapes: tuple[dict[str, object], ...]
    planned_stage_count: int
    planned_parallelizable_transactions: int
    speculative_wave_count: int
    speculative_accepted: int
    speculative_rejected: int
    serial_prefiltered: int
    serial_fallbacks: int
    guardrail_fallbacks: int


def _get_worker_runtime(
    *,
    storage_home: str,
    use_rewards_handler: bool,
    execution_runtime: VmRuntime | None = None,
) -> _WorkerRuntime:
    if execution_runtime is None:
        execution_runtime = VmRuntime()
    key = (
        storage_home,
        use_rewards_handler,
    )
    runtime = _WORKER_RUNTIMES.get(key)
    if runtime is not None:
        return runtime

    driver = Driver(storage_home=Path(storage_home), bypass_cache=True)
    client = ContractingClient(
        storage_home=Path(storage_home),
        driver=driver,
        submission_filename=None,
    )
    runtime = _WorkerRuntime(
        client=client,
        tx_processor=TxProcessor(
            client=client,
            execution_runtime=execution_runtime,
        ),
        rewards_handler=(RewardsHandler(client=client) if use_rewards_handler else None),
    )
    _WORKER_RUNTIMES[key] = runtime
    return runtime


def _speculative_process_tx(task: dict) -> dict:
    runtime = _get_worker_runtime(
        storage_home=task["storage_home"],
        use_rewards_handler=task["use_rewards_handler"],
        execution_runtime=task["execution_runtime"],
    )
    runtime.client.raw_driver.flush_cache()
    runtime.tx_processor.reset_block_cache()

    if task["base_pending_writes"]:
        runtime.client.raw_driver.apply_writes(task["base_pending_writes"])

    try:
        return runtime.tx_processor.process_tx(
            task["tx"],
            enabled_fees=task["enabled_fees"],
            fee_policy=task.get("fee_policy"),
            rewards_handler=runtime.rewards_handler,
            track_access=True,
        )
    finally:
        runtime.client.raw_driver.flush_cache()


def _warm_worker(task: dict) -> bool:
    _get_worker_runtime(
        storage_home=task["storage_home"],
        use_rewards_handler=task["use_rewards_handler"],
        execution_runtime=task["execution_runtime"],
    )
    return True


class ParallelBlockExecutor(SpeculativeExecutionController):
    def __init__(
        self,
        *,
        storage_home: str | Path,
        enabled: bool = False,
        workers: int = 0,
        min_transactions: int = 8,
        max_speculative_waves: int = 4,
        min_wave_acceptance_ratio: float = 0.25,
        low_acceptance_min_wave_size: int = 8,
        access_estimates_enabled: bool = True,
        execution_runtime: VmRuntime | None = None,
    ) -> None:
        self.storage_home = Path(storage_home)
        self.execution_runtime = execution_runtime or VmRuntime()
        self._mp_context = multiprocessing.get_context("spawn")
        self._executor: ProcessPoolExecutor | None = None
        self._batch_tx_processor: TxProcessor | None = None
        self._batch_enabled_fees = False
        self._batch_fee_policy: TxFeePolicy | None = None
        self._batch_rewards_handler: RewardsHandler | None = None
        super().__init__(
            enabled=enabled,
            workers=workers,
            min_batch_size=min_transactions,
            max_speculative_waves=max_speculative_waves,
            min_wave_acceptance_ratio=min_wave_acceptance_ratio,
            low_acceptance_min_wave_size=low_acceptance_min_wave_size,
            use_access_estimates=access_estimates_enabled,
        )

    def execute(
        self,
        *,
        txs: list[dict],
        tx_processor: TxProcessor,
        enabled_fees: bool,
        fee_policy: TxFeePolicy | None = None,
        rewards_handler: RewardsHandler | None = None,
    ) -> tuple[list[dict], ParallelExecutionStats] | None:
        if not self.is_enabled_for_batch(len(txs)):
            return None

        self._batch_tx_processor = tx_processor
        self._batch_enabled_fees = enabled_fees
        self._batch_fee_policy = fee_policy
        self._batch_rewards_handler = rewards_handler
        try:
            known_shapes, unknown_shapes = self._estimate_shape_summaries(
                txs=txs,
                tx_processor=tx_processor,
            )
            results = super().execute(requests=txs, auto_commit=False)
            final_results, stats = results
            return final_results, ParallelExecutionStats(
                worker_count=stats.worker_count,
                estimated_known_transactions=stats.estimated_known_requests,
                estimated_unknown_transactions=(stats.estimated_unknown_requests),
                estimated_stage_count=stats.estimated_stage_count,
                estimated_parallelizable_transactions=(stats.estimated_parallelizable_requests),
                estimated_known_shapes=known_shapes,
                estimated_unknown_shapes=unknown_shapes,
                planned_stage_count=stats.planned_stage_count,
                planned_parallelizable_transactions=(stats.planned_parallelizable_requests),
                speculative_wave_count=stats.speculative_wave_count,
                speculative_accepted=stats.speculative_accepted,
                speculative_rejected=stats.speculative_rejected,
                serial_prefiltered=stats.serial_prefiltered,
                serial_fallbacks=stats.serial_fallbacks,
                guardrail_fallbacks=stats.guardrail_fallbacks,
            )
        finally:
            self._batch_tx_processor = None
            self._batch_enabled_fees = False
            self._batch_fee_policy = None
            self._batch_rewards_handler = None

    def warm(self, *, use_rewards_handler: bool) -> None:
        if not self.enabled or self.workers <= 0:
            return

        tasks = [
            {
                "storage_home": str(self.storage_home),
                "use_rewards_handler": use_rewards_handler,
                "execution_runtime": self.execution_runtime,
            }
            for _ in range(self.workers)
        ]
        try:
            if self.workers == 1:
                _warm_worker(tasks[0])
                return
            list(self._get_executor().map(_warm_worker, tasks))
        except Exception:
            self.close()
            logger.exception("Failed to warm parallel execution workers")

    def close(self) -> None:
        if self._executor is None:
            return
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._executor = None

    def _handle_speculation_failure(self, _exc: Exception) -> None:
        logger.exception("Parallel speculation failed; falling back to serial block execution")

    def _get_base_pending_writes(self) -> dict[str, object]:
        assert self._batch_tx_processor is not None
        return deepcopy(self._batch_tx_processor.client.raw_driver.pending_writes)

    def _execute_serial_request(self, request: object) -> dict:
        assert self._batch_tx_processor is not None
        assert isinstance(request, dict)
        return self._batch_tx_processor.process_tx(
            request,
            enabled_fees=self._batch_enabled_fees,
            fee_policy=self._batch_fee_policy,
            rewards_handler=self._batch_rewards_handler,
            track_access=True,
        )

    def _speculate_many(
        self,
        *,
        requests: list[object],
        base_pending_writes: dict[str, object],
    ) -> list[dict]:
        tasks = [
            {
                "storage_home": str(self.storage_home),
                "tx": tx,
                "enabled_fees": self._batch_enabled_fees,
                "fee_policy": self._batch_fee_policy,
                "use_rewards_handler": self._batch_rewards_handler is not None,
                "execution_runtime": self.execution_runtime,
                "base_pending_writes": deepcopy(base_pending_writes),
            }
            for tx in requests
        ]

        if self.workers == 1:
            return [_speculative_process_tx(task) for task in tasks]

        return list(self._get_executor().map(_speculative_process_tx, tasks))

    def _get_executor(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=self._mp_context,
            )
        return self._executor

    def _normalize_access(
        self,
        *,
        index: int,
        request: object,
        output: dict | None,
    ) -> ExecutionAccess | None:
        if output is None:
            return None
        access = output.get("access")
        if access is None:
            return None
        return replace(access, index=index)

    def _get_request_sender(self, request: object) -> str | None:
        assert isinstance(request, dict)
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return None
        sender = payload.get("sender")
        if isinstance(sender, str):
            return sender
        return None

    def _estimate_shape_summaries(
        self,
        *,
        txs: list[dict],
        tx_processor: TxProcessor,
        limit: int = 16,
    ) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
        if not self.use_access_estimates:
            return (), ()

        known: Counter[tuple[str, str]] = Counter()
        unknown: Counter[tuple[str, str]] = Counter()
        for tx in txs:
            shape = self._tx_shape(tx)
            if tx_processor.estimate_access(tx) is None:
                unknown[shape] += 1
            else:
                known[shape] += 1

        return (
            self._format_shape_summary(known, limit=limit),
            self._format_shape_summary(unknown, limit=limit),
        )

    @staticmethod
    def _tx_shape(tx: dict) -> tuple[str, str]:
        payload = tx.get("payload")
        if not isinstance(payload, dict):
            return "<invalid>", "<invalid>"
        contract = payload.get("contract")
        function = payload.get("function")
        return (
            contract if isinstance(contract, str) else "<invalid>",
            function if isinstance(function, str) else "<invalid>",
        )

    @staticmethod
    def _format_shape_summary(
        counter: Counter[tuple[str, str]],
        *,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "contract": contract,
                "function": function,
                "count": count,
            }
            for (contract, function), count in sorted(
                counter.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1]),
            )[:limit]
        )

    def _estimate_access(
        self,
        *,
        index: int,
        request: object,
    ) -> ExecutionAccess | None:
        assert self._batch_tx_processor is not None
        assert isinstance(request, dict)
        access = self._batch_tx_processor.estimate_access(request)
        if access is None:
            return None
        return replace(access, index=index)

    def _apply_speculative_output(self, output: dict) -> None:
        assert self._batch_tx_processor is not None
        output["tx_result"]["state"] = self._batch_tx_processor.materialize_writes(
            output.get("base_writes", {}),
            output.get("reward_deltas", {}),
        )
        self._batch_tx_processor.update_chi_cost_cache(output.get("base_writes", {}))
        self._batch_tx_processor.apply_tx_result(output["tx_result"])
