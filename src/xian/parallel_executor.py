from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from contracting.client import ContractingClient
from contracting.storage.driver import Driver
from loguru import logger

from xian.parallel_planner import ParallelExecutionPlanner, TransactionAccess
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler


@dataclass
class _WorkerRuntime:
    client: ContractingClient
    tx_processor: TxProcessor
    rewards_handler: RewardsHandler | None


_WORKER_RUNTIMES: dict[tuple[str, bool], _WorkerRuntime] = {}


def _get_worker_runtime(
    *,
    storage_home: str,
    use_rewards_handler: bool,
) -> _WorkerRuntime:
    key = (storage_home, use_rewards_handler)
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
        tx_processor=TxProcessor(client=client),
        rewards_handler=(
            RewardsHandler(client=client) if use_rewards_handler else None
        ),
    )
    _WORKER_RUNTIMES[key] = runtime
    return runtime


def _speculative_process_tx(task: dict) -> dict:
    runtime = _get_worker_runtime(
        storage_home=task["storage_home"],
        use_rewards_handler=task["use_rewards_handler"],
    )
    runtime.client.raw_driver.flush_cache()
    runtime.tx_processor.reset_block_cache()

    try:
        return runtime.tx_processor.process_tx(
            task["tx"],
            enabled_fees=task["enabled_fees"],
            rewards_handler=runtime.rewards_handler,
            track_access=True,
        )
    finally:
        runtime.client.raw_driver.flush_cache()


@dataclass(frozen=True)
class ParallelExecutionStats:
    worker_count: int
    planned_stage_count: int
    planned_parallelizable_transactions: int
    speculative_accepted: int
    serial_fallbacks: int


class ParallelBlockExecutor:
    def __init__(
        self,
        *,
        storage_home: str | Path,
        enabled: bool = False,
        workers: int = 0,
        min_transactions: int = 8,
    ) -> None:
        self.storage_home = Path(storage_home)
        self.enabled = enabled
        self.workers = max(int(workers), 0)
        self.min_transactions = max(int(min_transactions), 1)
        self.planner = ParallelExecutionPlanner()
        self._mp_context = multiprocessing.get_context("spawn")
        self._executor: ProcessPoolExecutor | None = None

    def is_enabled_for_block(self, tx_count: int) -> bool:
        return (
            self.enabled
            and self.workers > 0
            and tx_count >= self.min_transactions
        )

    def execute(
        self,
        *,
        txs: list[dict],
        tx_processor: TxProcessor,
        enabled_fees: bool,
        rewards_handler: RewardsHandler | None,
    ) -> tuple[list[dict], ParallelExecutionStats] | None:
        if not self.is_enabled_for_block(len(txs)):
            return None

        try:
            speculative_results = self._speculate_many(
                txs=txs,
                enabled_fees=enabled_fees,
                rewards_handler=rewards_handler,
            )
        except Exception:
            self.close()
            logger.exception(
                "Parallel speculation failed; falling back to serial block execution"
            )
            return None

        accesses = [
            normalized
            for index, result in enumerate(speculative_results)
            if (
                normalized := self._normalize_access(
                    index=index, access=result.get("access")
                )
            )
        ]
        plan = self.planner.build(accesses) if accesses else None

        committed_writes: set[str] = set()
        committed_additive_writes: set[str] = set()
        committed_senders: set[str] = set()
        final_results: list[dict] = []
        speculative_accepted = 0
        serial_fallbacks = 0

        for index, tx in enumerate(txs):
            result = speculative_results[index]
            access = self._normalize_access(
                index=index,
                access=result.get("access"),
            )

            if self._should_fallback(
                result=result,
                access=access,
                committed_writes=committed_writes,
                committed_additive_writes=committed_additive_writes,
                committed_senders=committed_senders,
            ):
                result = tx_processor.process_tx(
                    tx,
                    enabled_fees=enabled_fees,
                    rewards_handler=rewards_handler,
                    track_access=True,
                )
                access = self._normalize_access(
                    index=index,
                    access=result.get("access"),
                )
                serial_fallbacks += 1
            else:
                result["tx_result"]["state"] = tx_processor.materialize_writes(
                    result.get("base_writes", {}),
                    result.get("reward_deltas", {}),
                )
                tx_processor.update_stamp_cost_cache(
                    result.get("base_writes", {})
                )
                tx_processor.apply_tx_result(result["tx_result"])
                speculative_accepted += 1

            final_results.append(result)

            if access is not None:
                committed_writes.update(access.writes)
                committed_additive_writes.update(access.additive_writes)
                committed_senders.add(access.sender)

        stats = ParallelExecutionStats(
            worker_count=self.workers,
            planned_stage_count=plan.stage_count if plan else 0,
            planned_parallelizable_transactions=(
                plan.parallelizable_transactions if plan else 0
            ),
            speculative_accepted=speculative_accepted,
            serial_fallbacks=serial_fallbacks,
        )
        return final_results, stats

    def _speculate_many(
        self,
        *,
        txs: list[dict],
        enabled_fees: bool,
        rewards_handler: RewardsHandler | None,
    ) -> list[dict]:
        tasks = [
            {
                "storage_home": str(self.storage_home),
                "tx": tx,
                "enabled_fees": enabled_fees,
                "use_rewards_handler": rewards_handler is not None,
            }
            for tx in txs
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

    def close(self) -> None:
        if self._executor is None:
            return
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._executor = None

    @staticmethod
    def _normalize_access(
        *, index: int, access: TransactionAccess | None
    ) -> TransactionAccess | None:
        if access is None:
            return None
        return replace(access, index=index)

    @staticmethod
    def _should_fallback(
        *,
        result: dict,
        access: TransactionAccess | None,
        committed_writes: set[str],
        committed_additive_writes: set[str],
        committed_senders: set[str],
    ) -> bool:
        tx_result = result.get("tx_result")
        if tx_result is None or access is None:
            return True

        if access.sender in committed_senders:
            return True

        if access.reads & committed_writes:
            return True

        if access.reads & committed_additive_writes:
            return True

        if ParallelBlockExecutor._prefix_conflicts(
            access.prefix_reads, committed_writes
        ):
            return True

        if ParallelBlockExecutor._prefix_conflicts(
            access.prefix_reads, committed_additive_writes
        ):
            return True

        if access.writes & committed_writes:
            return True

        if access.writes & committed_additive_writes:
            return True

        if access.additive_writes & committed_writes:
            return True

        return False

    @staticmethod
    def _prefix_conflicts(
        prefixes: frozenset[str], keys: set[str]
    ) -> bool:
        return any(key.startswith(prefix) for prefix in prefixes for key in keys)
