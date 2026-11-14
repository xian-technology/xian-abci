from contracting.runtime_features import RUNTIME_FEATURE_ZK, runtime_feature_without_state
from contracting.stdlib.bridge import zk as zk_bridge
from loguru import logger
from xian_runtime_types.encoding import safe_repr
from xian_vm_core import BlockExecutor, native_error_result

from xian.app_logging import build_log_fields
from xian.execution_policy import EXECUTION_POLICY_KEY
from xian.fee_policy import TxFeePolicy
from xian.utils.encoding import normalize_contract_result
from xian.utils.tx import transaction_bytes, tx_hash_from_tx


class TxProcessor:
    """Runs a block's transactions in the Rust block executor (vm-core
    ``BlockExecutor``) and turns each outcome into its ``process_tx``
    response: the receipt FinalizeBlock returns, with the per-transaction
    log records."""

    def __init__(self, client, trace_logging: bool = False):
        self.client = client
        self.trace_logging = trace_logging

    def reset_block_cache(self) -> None:
        zk_bridge.clear_verified_proof_cache()

    def process_system_tx(self, tx) -> dict:
        """The ``process_tx`` response for a system transaction (epoch
        rebalance, evidence penalty): unmetered, no fees, no rewards."""
        [response] = self.process_block(
            [tx], [transaction_bytes(tx)], fee_policy=TxFeePolicy.system_unmetered()
        )
        return response

    def process_block(self, txs, raw_txs, *, fee_policy: TxFeePolicy) -> list[dict]:
        """``process_tx`` responses for a block's transactions, in order.

        ``raw_txs`` are the transactions' wire bytes (``txs`` decoded, with
        ``b_meta``). The transactions run in Rust over the block overlay with
        the GIL released; Rust reads each one's call, arguments and context
        from its bytes and gives every one a receipt. Only a host fault
        raises (the node halts). The fee policy decides metering, fees and
        fee rewards.
        """
        if not txs:
            return []
        outcomes = self._block_executor(txs, raw_txs, fee_policy).run()
        return [
            self._native_response(tx, outcome, fee_policy)
            for tx, outcome in zip(txs, outcomes, strict=True)
        ]

    def _block_executor(self, txs, raw_txs, fee_policy: TxFeePolicy):
        environment, overlay = self.client.raw_driver.committed_storage()
        executor = BlockExecutor(
            environment,
            overlay,
            meter_execution=fee_policy.meter_execution,
            charge_fees=fee_policy.charge_fees,
            distribute_fee_rewards=fee_policy.distribute_fee_rewards,
            protected_keys=[EXECUTION_POLICY_KEY],
            zk_enabled_without_state=runtime_feature_without_state(
                self.client.raw_driver, RUNTIME_FEATURE_ZK
            ),
            block_meta=txs[0]["b_meta"],
        )
        for raw in raw_txs:
            executor.add(raw)
        return executor

    def _native_response(self, tx, outcome, fee_policy: TxFeePolicy) -> dict:
        """The ``process_tx`` response of a transaction, with its log records."""
        (status, result, exception, writes, events, chi_used, rewards, chi_rate, output_writes) = (
            outcome
        )
        if self.trace_logging:
            self._log_execution(tx, chi_rate, fee_policy.meter_execution)
        if status:
            result = native_error_result(status, result, exception=exception)
        self._log_output(tx, status, chi_used, output_writes, result)
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
        if self.trace_logging:
            self._log_result(tx, tx_result)
        return {
            "tx_result": tx_result,
            "chi_rewards_amount": chi_used,
            "chi_rewards_contract": tx["payload"]["contract"],
        }

    # The per-transaction log records. The DEBUG records need trace logging.

    @staticmethod
    def _log_execution(tx, chi_cost, metering: bool) -> None:
        logger.bind(
            **build_log_fields(
                stage="execute_tx",
                tx=tx,
                extra={"chi_cost": chi_cost, "metering": metering},
            )
        ).debug("Executing transaction")

    def _log_output(self, tx, status: int, chi_used, write_count: int, result) -> None:
        """The executor output; a failure is a warning whatever the level."""
        extra = {"chi_used": chi_used, "write_count": write_count}
        if self.trace_logging:
            logger.bind(
                **build_log_fields(stage="process_tx_output", tx=tx, status=status, extra=extra)
            ).debug("Processing executor output")
        if status > 0:
            logger.bind(
                **build_log_fields(
                    stage="process_tx_output",
                    tx=tx,
                    status=status,
                    extra={**extra, "error_type": type(result).__name__},
                )
            ).warning("Transaction execution returned a non-zero status")

    @staticmethod
    def _log_result(tx, tx_result: dict) -> None:
        logger.bind(
            **build_log_fields(
                stage="tx_result",
                tx=tx,
                tx_hash=tx_result["hash"],
                status=tx_result["status"],
                extra={
                    "chi_used": tx_result["chi_used"],
                    "state_write_count": len(tx_result["state"]),
                    "event_count": len(tx_result["events"]),
                },
            )
        ).debug("Produced transaction result")
