import hashlib
import time

from contracting.execution.executor import Executor
from contracting.execution.parallel import ExecutionAccess as TransactionAccess
from contracting.stdlib.bridge import zk as zk_bridge
from loguru import logger
from xian_runtime_types.encoding import convert_dict, safe_repr
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
from xian.utils.block import nanoseconds_to_utc_datetime
from xian.utils.tx import canonical_transaction_size_bytes, tx_hash_from_tx


class TxProcessor:
    def __init__(
        self,
        client,
        metering=False,
        profiler=None,
        trace_logging: bool = False,
        execution_runtime: ExecutionRuntime | None = None,
    ):
        self.client = client
        self.profiler = profiler
        self.trace_logging = trace_logging
        self.execution_runtime = execution_runtime or ExecutionRuntime(
            mode="python_line_v1",
            tracer_mode="python_line_v1",
        )
        self.executor = Executor(
            driver=self.client.raw_driver, metering=metering
        )
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
            self.cached_chi_cost = (
                self.client.get_var(
                    contract="chi_cost",
                    variable="S",
                    arguments=["value"],
                )
                or 1
            )
        return self.cached_chi_cost

    def update_chi_cost_cache(self, base_writes: dict) -> None:
        if self.chi_cost_key in base_writes:
            self.cached_chi_cost = base_writes[self.chi_cost_key]

    def process_tx(
        self,
        tx,
        enabled_fees=False,
        rewards_handler=None,
        *,
        track_access: bool = False,
    ):
        started_ns = time.perf_counter_ns()
        driver = self.client.raw_driver
        previous_read_tracking = driver.track_transaction_reads
        driver.set_transaction_read_tracking(track_access)
        environment = self.get_environment(tx=tx)
        chi_cost = self.get_chi_cost()

        try:
            # Execute the transaction
            output = self.execute_tx(
                transaction=tx,
                chi_cost=chi_cost,
                environment=environment,
                metering=enabled_fees,
            )
            if output is None:
                return {
                    "tx_result": None,
                    "chi_rewards_amount": 0,
                    "chi_rewards_contract": None,
                    "base_writes": {},
                    "reward_deltas": {},
                    "access": None,
                }

            # Process the result of the executor
            processed = self.process_tx_output(
                output=output,
                transaction=tx,
                chi_cost=chi_cost,
                rewards_handler=rewards_handler,
                track_access=track_access,
            )
            tx_result = processed["tx_result"]
            self.update_chi_cost_cache(processed["base_writes"])
            tx_result = self.prune_tx_result(tx_result)

            response = {
                "tx_result": tx_result,
                "chi_rewards_amount": output["chi_used"],
                "chi_rewards_contract": tx["payload"]["contract"],
            }
            if track_access:
                response.update(
                    {
                        "base_writes": processed["base_writes"],
                        "reward_deltas": processed["reward_deltas"],
                        "access": self.build_access_record(
                            tx=tx,
                            status_code=output["status_code"],
                            reads=processed["reads"],
                            prefix_reads=processed["prefix_reads"],
                            base_writes=processed["base_writes"],
                            reward_deltas=processed["reward_deltas"],
                        ),
                    }
                )
            return response
        except Exception as e:
            logger.bind(
                **build_log_fields(
                    stage="process_tx",
                    tx=tx,
                    extra={"error_type": type(e).__name__},
                )
            ).exception("Transaction processing failed unexpectedly")
            response = {
                "tx_result": None,
                "chi_rewards_amount": 0,
                "chi_rewards_contract": None,
            }
            if track_access:
                response.update(
                    {
                        "base_writes": {},
                        "reward_deltas": {},
                        "access": None,
                    }
                )
            return response
        finally:
            driver.set_transaction_read_tracking(previous_read_tracking)
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_process_total",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def execute_tx(
        self, transaction, chi_cost, environment: dict = {}, metering=False
    ):
        execution_runtime = getattr(
            self,
            "execution_runtime",
            ExecutionRuntime(
                mode="python_line_v1",
                tracer_mode="python_line_v1",
            ),
        )
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
            prepare_contract_for_execution(
                execution_runtime,
                self.client.raw_driver,
                transaction["payload"]["contract"],
            )
            converted_kwargs = convert_dict(transaction["payload"]["kwargs"])
            if xian_vm_requires_deployment_artifacts(
                execution_runtime,
                transaction["payload"]["contract"],
                transaction["payload"]["function"],
                converted_kwargs,
            ):
                return {
                    "status_code": 1,
                    "result": ValueError(
                        xian_vm_deployment_artifacts_error(
                            transaction["payload"]["contract"],
                            transaction["payload"]["function"],
                        )
                    ),
                    "writes": {},
                    "events": [],
                    "chi_used": 0,
                    "reads": {},
                    "prefix_reads": frozenset(),
                    "contract_costs": {},
                }
            if getattr(execution_runtime, "native_authoritative", False):
                return self._execute_native_authoritative_tx(
                    transaction=transaction,
                    kwargs=converted_kwargs,
                    chi_cost=chi_cost,
                    environment=environment,
                    metering=metering,
                )
            pre_execution_state = None
            if getattr(execution_runtime, "shadow_execution", False):
                pre_execution_state = snapshot_driver_state(
                    self.client.raw_driver
                )
            # Execute transaction
            output = self.executor.execute(
                sender=transaction["payload"]["sender"],
                contract_name=transaction["payload"]["contract"],
                function_name=transaction["payload"]["function"],
                chi=transaction["payload"]["chi_supplied"],
                chi_cost=chi_cost,
                kwargs=converted_kwargs,
                environment=environment,
                auto_commit=False,
                metering=metering,
                transaction_size_bytes=canonical_transaction_size_bytes(
                    transaction
                ),
            )
            if (
                getattr(execution_runtime, "shadow_execution", False)
                and pre_execution_state
            ):
                python_driver_state = snapshot_driver_state(
                    self.client.raw_driver
                )
                restore_driver_state(
                    self.client.raw_driver, pre_execution_state
                )
                native_output = execute_native_contract(
                    execution_runtime,
                    self.client.raw_driver,
                    sender=transaction["payload"]["sender"],
                    contract_name=transaction["payload"]["contract"],
                    function_name=transaction["payload"]["function"],
                    kwargs=converted_kwargs,
                    environment=environment,
                    meter=metering,
                    chi_budget=transaction["payload"]["chi_supplied"],
                    transaction_size_bytes=canonical_transaction_size_bytes(
                        transaction
                    ),
                )
                restore_driver_state(
                    self.client.raw_driver, python_driver_state
                )
                output_for_compare = augment_execution_output_with_driver_state(
                    output,
                    before_state=pre_execution_state,
                    after_state=python_driver_state,
                )
                mismatches = compare_execution_results(
                    output_for_compare,
                    native_output,
                    ignore_write_keys=metering_write_keys(
                        self.client.raw_driver,
                        sender=transaction["payload"]["sender"],
                        currency_contract=getattr(
                            self.executor, "currency_contract", "currency"
                        ),
                        balances_hash=getattr(
                            self.executor, "balances_hash", "balances"
                        ),
                    ),
                )
                if mismatches:
                    logger.bind(
                        **build_log_fields(
                            stage="execute_tx_native_shadow",
                            tx=transaction,
                            extra={
                                "mismatch_fields": sorted(mismatches),
                            },
                        )
                    ).warning(
                        "Native VM transaction shadow mismatch: {}",
                        mismatches,
                )
            return output
        except (TypeError, ValueError) as err:
            logger.bind(
                **build_log_fields(
                    stage="execute_tx",
                    tx=transaction,
                    extra={
                        "chi_cost": chi_cost,
                        "chi_supplied": transaction["payload"]["chi_supplied"],
                        "metering": metering,
                        "error_type": type(err).__name__,
                    },
                )
            ).exception(
                "Transaction execution failed before producing an executor result"
            )
            return None
        finally:
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_execute",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def _execute_native_authoritative_tx(
        self,
        *,
        transaction: dict,
        kwargs: dict,
        chi_cost: int,
        environment: dict,
        metering: bool,
    ) -> dict:
        driver = self.client.raw_driver
        if hasattr(driver, "clear_transaction_reads"):
            driver.clear_transaction_reads()
        else:
            getattr(driver, "transaction_reads", {}).clear()
            getattr(driver, "transaction_read_prefixes", set()).clear()
        if hasattr(driver, "clear_transaction_writes"):
            driver.clear_transaction_writes()
        else:
            getattr(driver, "transaction_writes", {}).clear()
        outcome = execute_authoritative_native_contract(
            self.execution_runtime,
            driver,
            executor=self.executor,
            sender=transaction["payload"]["sender"],
            contract_name=transaction["payload"]["contract"],
            function_name=transaction["payload"]["function"],
            kwargs=kwargs,
            environment=environment,
            chi_budget=transaction["payload"]["chi_supplied"],
            chi_cost=chi_cost,
            meter=metering,
            transaction_size_bytes=canonical_transaction_size_bytes(transaction),
            mismatch_label="native authoritative execution",
        )

        return {
            "status_code": outcome.output.status_code,
            "result": outcome.output.result,
            "writes": outcome.writes,
            "events": list(outcome.output.events),
            "chi_used": outcome.chi_used,
            "reads": dict(getattr(outcome, "reads", {}) or {}),
            "prefix_reads": frozenset(
                getattr(outcome, "prefix_reads", frozenset()) or ()
            ),
            "contract_costs": outcome.contract_costs,
        }

    def process_tx_output(
        self,
        output,
        transaction,
        chi_cost,
        rewards_handler,
        *,
        track_access: bool = False,
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
                            "result": safe_repr(output["result"]),
                        },
                    )
                ).warning("Transaction execution returned a non-zero status")

            tx_hash = tx_hash_from_tx(transaction)

            rewards = None
            reward_deltas = {}
            reward_records = []
            if output["status_code"] == 0 and rewards_handler is not None:
                rewards, reward_deltas, reward_records = (
                    rewards_handler.build_tx_reward_outputs(
                        total_chi_to_split=output["chi_used"],
                        contract=transaction["payload"]["contract"],
                        contract_costs=output.get("contract_costs"),
                    )
                )

            base_writes = self.determine_writes_from_output(
                status_code=output["status_code"],
                ouput_writes=output["writes"],
                chi_used=output["chi_used"],
                chi_cost=chi_cost,
                tx_sender=transaction["payload"]["sender"],
            )
            writes = self.materialize_writes(base_writes, reward_deltas)
            reads = frozenset()
            prefix_reads = frozenset()
            if track_access:
                output_reads = output.get("reads")
                if isinstance(output_reads, dict):
                    reads = frozenset(output_reads.keys())
                elif output_reads is not None:
                    reads = frozenset(output_reads)
                else:
                    reads = frozenset(
                        self.client.raw_driver.transaction_reads.keys()
                    )

                output_prefix_reads = output.get("prefix_reads")
                if output_prefix_reads is not None:
                    prefix_reads = frozenset(output_prefix_reads)
                else:
                    prefix_reads = frozenset(
                        self.client.raw_driver.transaction_read_prefixes
                    )

            for write in writes:
                self.client.raw_driver.set(
                    key=write["key"], value=write["value"]
                )

            tx_output = {
                "hash": tx_hash,
                "status": output["status_code"],
                "state": writes,
                "events": output["events"],
                "chi_used": output["chi_used"],
                "result": safe_repr(output["result"]),
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
                "reads": reads,
                "prefix_reads": prefix_reads,
                "base_writes": base_writes,
                "reward_deltas": reward_deltas,
            }
        finally:
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_process_output",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def apply_tx_result(self, tx_result: dict) -> None:
        for write in tx_result["state"]:
            self.client.raw_driver.set(key=write["key"], value=write["value"])

    def determine_writes_from_output(
        self,
        status_code,
        ouput_writes,
        chi_used,
        chi_cost,
        tx_sender,
    ):
        # Only apply the writes if the tx passes
        if status_code == 0:
            return dict(ouput_writes)
        else:
            sender_balance = self.executor.driver.get_var(
                contract="currency",
                variable="balances",
                arguments=[tx_sender],
                mark=False,
            )

            # Calculate only chi deductions
            to_deduct = chi_used / chi_cost
            new_bal = 0
            try:
                new_bal = sender_balance - to_deduct
            except TypeError:
                pass
            else:
                if new_bal <= 0:
                    new_bal = 0

            return {f"currency.balances:{tx_sender}": new_bal}

    def materialize_writes(self, base_writes, reward_deltas):
        writes_map = dict(base_writes)

        for key, delta in reward_deltas.items():
            if key in writes_map:
                writes_map[key] += delta
                continue

            current_value = self.client.raw_driver.get(key, save=False)
            if current_value is None:
                current_value = 0
            writes_map[key] = current_value + delta

        writes = [{"key": k, "value": v} for k, v in writes_map.items()]
        try:
            writes.sort(key=lambda x: x["key"])
        except Exception as e:
            logger.error(f"Unable to sort state writes by 'key': {e}")

        return writes

    def get_environment(self, tx):
        block_meta = tx["b_meta"]
        nanos = block_meta["nanos"]
        signature = tx["metadata"]["signature"]
        chain_id = block_meta["chain_id"]

        # Nanos is set at the time of block being processed, and is shared between all txns in a block.
        # TODO : confirm this w/ CometBFT docs.
        # it's a deterministic value which is the average of times from validators who voted for this block
        # it's set during the consensus agreement & voting for block between all validators.

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

    def build_access_record(
        self,
        tx: dict,
        status_code: int,
        reads: frozenset[str],
        prefix_reads: frozenset[str],
        base_writes: dict,
        reward_deltas: dict,
    ) -> TransactionAccess:
        return TransactionAccess(
            index=-1,
            sender=tx["payload"]["sender"],
            nonce=tx["payload"].get("nonce", 0),
            reads=reads,
            prefix_reads=prefix_reads,
            writes=frozenset(base_writes.keys()),
            additive_writes=frozenset(reward_deltas.keys()),
            status=status_code,
        )
