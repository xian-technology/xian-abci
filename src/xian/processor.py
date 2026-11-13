import hashlib
import time

from contracting.stdlib.bridge import zk as zk_bridge
from loguru import logger
from xian_runtime_types.encoding import decode_wire_numbers, safe_repr
from xian_runtime_types.time import Datetime
from xian_vm_core import BlockExecutor, coerce_call_kwargs, native_error_result

from xian.app_logging import build_log_fields
from xian.execution_engine import (
    HOST_FAILURE_MESSAGE,
    HOST_FAULT_EXCEPTIONS,
    VM_EXECUTION_MODE,
    VmRuntime,
    execute_vm_transaction,
    failed_transaction_chi,
    vm_metering_writes,
)
from xian.execution_policy import EXECUTION_POLICY_KEY
from xian.fee_policy import TxFeePolicy, chain_chi_rate
from xian.utils.block import nanoseconds_to_utc_datetime
from xian.utils.encoding import normalize_contract_result
from xian.utils.tx import canonical_transaction_size_bytes, tx_hash_from_tx


class TxProcessor:
    def __init__(
        self,
        client,
        metering=False,
        profiler=None,
        trace_logging: bool = False,
        execution_runtime: VmRuntime | None = None,
    ):
        self.client = client
        self.profiler = profiler
        self.trace_logging = trace_logging
        self.execution_runtime = execution_runtime or VmRuntime()
        self.currency_contract = "currency"
        self.balances_hash = "balances"
        self.chi_cost_key = self.client.raw_driver.make_key(
            "chi_cost",
            "S",
            ["value"],
        )
        self.cached_chi_cost = None

    def reset_block_cache(self) -> None:
        self.cached_chi_cost = None
        zk_bridge.clear_verified_proof_cache()

    def get_chi_cost(self):
        if self.cached_chi_cost is None:
            self.cached_chi_cost = chain_chi_rate(self.client)
        return self.cached_chi_cost

    def update_chi_cost_cache(self, base_writes: dict) -> None:
        if self.chi_cost_key in base_writes:
            self.cached_chi_cost = base_writes[self.chi_cost_key]

    def process_tx(
        self,
        tx,
        rewards_handler=None,
        *,
        fee_policy: TxFeePolicy | None = None,
    ):
        started_ns = time.perf_counter_ns()
        environment = self.get_environment(tx=tx)
        fee_policy = fee_policy or TxFeePolicy.system_unmetered()
        # Only a fee-charging policy prices chi; the others never use the rate.
        chi_cost = self.get_chi_cost() if fee_policy.charge_fees else None

        try:
            try:
                output = self.execute_tx(
                    transaction=tx,
                    chi_cost=chi_cost,
                    environment=environment,
                    metering=fee_policy.meter_execution,
                    charge_fees=fee_policy.charge_fees,
                )
                processed = self._process_output(output, tx, chi_cost, rewards_handler, fee_policy)
            except HOST_FAULT_EXCEPTIONS:
                raise
            except Exception as exc:
                logger.bind(
                    **build_log_fields(
                        stage="process_tx",
                        tx=tx,
                        extra={"error_type": type(exc).__name__},
                    )
                ).exception("Transaction processing failed; recording a failed receipt")
                output = self._unexpected_failure_output(tx, fee_policy)
                processed = self._process_output(output, tx, chi_cost, rewards_handler, fee_policy)

            tx_result = processed["tx_result"]
            self.update_chi_cost_cache(processed["base_writes"])
            tx_result = self.prune_tx_result(tx_result)

            return {
                "tx_result": tx_result,
                "chi_rewards_amount": output["chi_used"],
                "chi_rewards_contract": tx["payload"]["contract"],
            }
        finally:
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_process_total",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def process_block(self, txs, *, fee_policy: TxFeePolicy, rewards_handler=None) -> list[dict]:
        """``process_tx`` responses for a block's transactions, in order.

        A metered chain policy runs the transactions in Rust over the block
        overlay with the GIL released; each transaction that needs Python
        (see ``xian_vm_core.BlockExecutor``) runs through ``process_tx`` in
        its place in the block, and Rust continues after it. The outcomes are
        byte-identical either way.
        """
        executor = self._block_executor(txs, fee_policy, rewards_handler)
        if executor is None:
            return [self.process_tx(tx, rewards_handler, fee_policy=fee_policy) for tx in txs]
        responses = []
        start = 0
        try:
            while start < len(txs):
                outcomes, stop = executor.run(start)
                for offset, outcome in enumerate(outcomes):
                    responses.append(self._native_response(txs[start + offset], outcome))
                if stop is None:
                    break
                index, reason = stop
                logger.bind(
                    **build_log_fields(
                        stage="process_block", tx=txs[index], extra={"reason": reason}
                    )
                ).debug("Transaction runs on the Python path")
                # The transactions Rust executed may have changed the chi rate.
                self.cached_chi_cost = None
                responses.append(
                    self.process_tx(txs[index], rewards_handler, fee_policy=fee_policy)
                )
                start = index + 1
        finally:
            self.cached_chi_cost = None
        return responses

    def _block_executor(self, txs, fee_policy: TxFeePolicy, rewards_handler):
        storage = self.client.raw_driver.committed_storage()
        if storage is None or not fee_policy.meter_execution:
            return None
        environment, overlay = storage
        executor = BlockExecutor(
            environment,
            overlay,
            charge_fees=fee_policy.charge_fees,
            distribute_fee_rewards=(
                fee_policy.distribute_fee_rewards and rewards_handler is not None
            ),
            protected_keys=[EXECUTION_POLICY_KEY],
        )
        for tx in txs:
            payload = tx["payload"]
            environment = self.get_environment(tx=tx)
            try:
                kwargs = coerce_call_kwargs(decode_wire_numbers(payload["kwargs"]))
            except Exception:  # noqa: BLE001 - process_tx records this failure
                kwargs = None
            executor.add(
                payload["sender"],
                payload["contract"],
                payload["function"],
                kwargs,
                {
                    "signer": payload["sender"],
                    "caller": payload["sender"],
                    "this": payload["contract"],
                    "entry": (payload["contract"], payload["function"]),
                    "submission_name": None,
                    "now": environment["now"],
                    "block_num": environment["block_num"],
                    "block_hash": environment["block_hash"],
                    "chain_id": environment["chain_id"],
                },
                payload["chi_supplied"],
                canonical_transaction_size_bytes(tx),
            )
        return executor

    def _native_response(self, tx, outcome) -> dict:
        """The ``process_tx`` response for a transaction executed in Rust."""
        status, result, exception, writes, events, chi_used, rewards = outcome
        if status:
            result = native_error_result(
                status, result, vm_profile=VM_EXECUTION_MODE, exception=exception
            )
            logger.bind(
                **build_log_fields(
                    stage="process_tx_output",
                    tx=tx,
                    status=status,
                    extra={
                        "chi_used": chi_used,
                        "write_count": len(writes),
                        "error_type": type(result).__name__,
                    },
                )
            ).warning("Transaction execution returned a non-zero status")
        rewards, _reward_deltas, reward_records = rewards or (None, {}, [])
        tx_result = {
            "hash": tx_hash_from_tx(tx),
            "status": status,
            "state": [{"key": key, "value": value} for key, value in writes],
            "events": events,
            "chi_used": chi_used,
            "result": safe_repr(result) if status else normalize_contract_result(result),
            "rewards": rewards if rewards else None,
            "reward_records": reward_records or None,
        }
        return {
            "tx_result": self.prune_tx_result(tx_result),
            "chi_rewards_amount": chi_used,
            "chi_rewards_contract": tx["payload"]["contract"],
        }

    def _process_output(self, output, tx, chi_cost, rewards_handler, fee_policy):
        return self.process_tx_output(
            output=output,
            transaction=tx,
            chi_cost=chi_cost,
            rewards_handler=rewards_handler,
            charge_fees=fee_policy.charge_fees,
            distribute_fee_rewards=fee_policy.distribute_fee_rewards,
        )

    @staticmethod
    def _unexpected_failure_output(tx: dict, fee_policy: TxFeePolicy) -> dict:
        chi_used = 0
        if fee_policy.meter_execution:
            chi_used = failed_transaction_chi(0, tx["payload"]["chi_supplied"])
        return {
            "status_code": 1,
            "result": RuntimeError(HOST_FAILURE_MESSAGE),
            "writes": {},
            "events": [],
            "chi_used": chi_used,
            "contract_costs": {},
        }

    def execute_tx(
        self,
        transaction,
        chi_cost,
        environment: dict | None = None,
        metering=False,
        charge_fees: bool = True,
    ):
        resolved_environment = environment or {}
        if self.trace_logging:
            logger.bind(
                **build_log_fields(
                    stage="execute_tx",
                    tx=transaction,
                    extra={
                        "chi_cost": chi_cost,
                        "metering": metering,
                    },
                )
            ).debug("Executing transaction")
        started_ns = time.perf_counter_ns()

        try:
            return self._execute_vm_tx(
                transaction=transaction,
                kwargs=decode_wire_numbers(transaction["payload"]["kwargs"]),
                chi_cost=chi_cost,
                environment=resolved_environment,
                metering=metering,
                charge_fees=charge_fees,
            )
        finally:
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_execute",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def _execute_vm_tx(
        self,
        *,
        transaction: dict,
        kwargs: dict,
        chi_cost: int,
        environment: dict,
        metering: bool,
        charge_fees: bool,
    ) -> dict:
        driver = self.client.raw_driver
        driver.clear_transaction_writes()
        outcome = execute_vm_transaction(
            self.execution_runtime,
            driver,
            sender=transaction["payload"]["sender"],
            contract_name=transaction["payload"]["contract"],
            function_name=transaction["payload"]["function"],
            kwargs=kwargs,
            environment=environment,
            chi_budget=transaction["payload"]["chi_supplied"],
            chi_cost=chi_cost,
            meter=metering,
            transaction_size_bytes=canonical_transaction_size_bytes(transaction),
            apply_metering_writes=charge_fees,
            currency_contract=self.currency_contract,
            balances_hash=self.balances_hash,
        )

        return {
            "status_code": outcome.output.status_code,
            "result": outcome.output.result,
            "writes": outcome.writes,
            "events": list(outcome.output.events),
            "chi_used": outcome.chi_used,
            "contract_costs": outcome.contract_costs,
        }

    def process_tx_output(
        self,
        output,
        transaction,
        chi_cost,
        rewards_handler,
        *,
        charge_fees: bool = True,
        distribute_fee_rewards: bool = True,
    ):
        started_ns = time.perf_counter_ns()
        try:
            if self.trace_logging:
                logger.bind(
                    **build_log_fields(
                        stage="process_tx_output",
                        tx=transaction,
                        status=output["status_code"],
                        extra={
                            "chi_used": output["chi_used"],
                            "write_count": len(output["writes"]),
                        },
                    )
                ).debug("Processing executor output")
            if output["status_code"] > 0:
                logger.bind(
                    **build_log_fields(
                        stage="process_tx_output",
                        tx=transaction,
                        status=output["status_code"],
                        extra={
                            "chi_used": output["chi_used"],
                            "write_count": len(output["writes"]),
                            "error_type": type(output["result"]).__name__,
                        },
                    )
                ).warning("Transaction execution returned a non-zero status")

            tx_hash = tx_hash_from_tx(transaction)

            rewards = None
            reward_deltas = {}
            reward_records = []
            if (
                distribute_fee_rewards
                and output["status_code"] == 0
                and rewards_handler is not None
            ):
                rewards, reward_deltas, reward_records = rewards_handler.build_tx_reward_outputs(
                    total_chi_to_split=output["chi_used"],
                    contract=transaction["payload"]["contract"],
                    contract_costs=output.get("contract_costs"),
                )

            base_writes = self.determine_writes_from_output(
                status_code=output["status_code"],
                ouput_writes=output["writes"],
                chi_used=output["chi_used"],
                chi_cost=chi_cost,
                tx_sender=transaction["payload"]["sender"],
                charge_fees=charge_fees,
            )
            writes = self.materialize_writes(base_writes, reward_deltas)
            self._stage_writes(writes)

            tx_output = {
                "hash": tx_hash,
                "status": output["status_code"],
                "state": writes,
                "events": output["events"],
                "chi_used": output["chi_used"],
                "result": (
                    safe_repr(output["result"])
                    if output["status_code"]
                    else normalize_contract_result(output["result"])
                ),
                "rewards": rewards if rewards else None,
                "reward_records": reward_records or None,
            }

            if self.trace_logging:
                logger.bind(
                    **build_log_fields(
                        stage="tx_result",
                        tx=transaction,
                        tx_hash=tx_hash,
                        status=output["status_code"],
                        extra={
                            "chi_used": output["chi_used"],
                            "state_write_count": len(writes),
                            "event_count": len(output["events"]),
                        },
                    )
                ).debug("Produced transaction result")

            return {
                "tx_result": tx_output,
                "base_writes": base_writes,
            }
        finally:
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_process_output",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def _stage_writes(self, writes: list[dict]) -> None:
        # All or nothing: apply_writes checks every key and value before it
        # stages any, so a failure never leaves part of a transaction behind.
        self.client.raw_driver.apply_writes({write["key"]: write["value"] for write in writes})

    def determine_writes_from_output(
        self,
        status_code,
        ouput_writes,
        chi_used,
        chi_cost,
        tx_sender,
        charge_fees=True,
    ):
        # Only apply the writes if the tx passes
        if status_code == 0:
            return dict(ouput_writes)
        if not charge_fees:
            return {}
        else:
            return vm_metering_writes(
                self.client.raw_driver,
                sender=tx_sender,
                chi_used=chi_used,
                chi_cost=chi_cost,
                currency_contract=self.currency_contract,
                balances_hash=self.balances_hash,
            )

    def materialize_writes(self, base_writes, reward_deltas):
        writes_map = dict(base_writes)

        for key, delta in reward_deltas.items():
            if key in writes_map:
                writes_map[key] += delta
                continue

            current_value = self.client.raw_driver.get(key)
            if current_value is None:
                current_value = 0
            writes_map[key] = current_value + delta

        writes = [{"key": k, "value": v} for k, v in writes_map.items()]
        # Write ordering is consensus-critical: every validator must apply
        # state in the same order, so a sort failure has to halt the node.
        writes.sort(key=lambda x: x["key"])

        return writes

    def get_environment(self, tx):
        block_meta = tx["b_meta"]
        nanos = block_meta["nanos"]
        signature = tx["metadata"]["signature"]
        chain_id = block_meta["chain_id"]

        # Nanos is derived from the block header time consensus agreed on, so
        # it is identical for every tx in the block and deterministic across
        # validators. CometBFT computes header time as the weighted median of
        # validator precommit timestamps (BFT Time), or takes the proposer's
        # timestamp when Proposer-Based Timestamps are enabled.

        return {
            "block_hash": block_meta["hash"],  # hash nanos
            "block_num": block_meta["height"],  # block number
            "__input_hash": self.get_timestamp_hash_from_tx(nanos, signature),
            "__xian_execution_mode__": self.execution_runtime.mode,
            "now": self.get_now_from_nanos(nanos=nanos),
            "chain_id": chain_id,
        }

    def get_timestamp_hash_from_tx(self, nanos, signature):
        h = hashlib.sha3_256()
        h.update("{}".format(str(nanos) + signature).encode())
        return h.hexdigest()

    def get_now_from_nanos(self, nanos):
        block_time = nanoseconds_to_utc_datetime(nanos)
        return Datetime._from_datetime(block_time)

    def prune_tx_result(self, tx_result: dict):
        return tx_result
