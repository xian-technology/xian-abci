import decimal
import os
import tempfile
import textwrap
import time
import unittest
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from contracting.client import ContractingClient

from xian.parallel_executor import (
    _WORKER_RUNTIMES,
    ParallelBlockExecutor,
    _get_worker_runtime,
)
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler
from xian.utils.encoding import stringify_decimals


def create_block_meta(dt: datetime = datetime.now()):
    nanos = int(time.mktime(dt.timetuple()) * 1e9 + dt.microsecond * 1e3)
    return {
        "nanos": nanos,
        "height": 123456,
        "chain_id": "test-chain",
        "hash": "abc123def456",
    }


class TestParallelBlockExecutor(unittest.TestCase):
    def _build_processor(
        self, storage_home: Path, *, with_rewards: bool = False
    ):
        client = ContractingClient(storage_home=storage_home)
        tx_processor = TxProcessor(client=client)

        contract_dir = (
            Path(os.path.dirname(os.path.abspath(__file__))) / "contracts"
        )
        token_code = (contract_dir / "token_contract.py").read_text(
            encoding="utf-8"
        )

        if with_rewards:
            client.submit(token_code, name="currency", signer="sys")
            client.raw_driver.set("stamp_cost.S:value", 100)
            client.raw_driver.set("foundation.owner", "foundation")
            client.raw_driver.set("masternodes.nodes", ["mn-1", "mn-2"])
            client.raw_driver.set("rewards.S:value", [0.88, 0.01, 0.01, 0.1])
            for address in (
                "alice",
                "bob",
                "carol",
                "foundation",
                "mn-1",
                "mn-2",
            ):
                client.raw_driver.set(f"currency.balances:{address}", 1000)

        client.submit(token_code, name="con_token_a", signer="sys")
        client.submit(token_code, name="con_token_b", signer="bob")
        client.submit(token_code, name="con_token_c", signer="carol")
        client.raw_driver.commit()

        return client, tx_processor

    @staticmethod
    def _tx(
        *,
        sender: str,
        contract: str,
        function: str,
        kwargs: dict,
        nonce: int,
        signature: str,
    ) -> dict:
        return {
            "payload": {
                "sender": sender,
                "contract": contract,
                "function": function,
                "kwargs": kwargs,
                "nonce": nonce,
                "stamps_supplied": 1000,
            },
            "metadata": {"signature": signature},
            "b_meta": create_block_meta(),
        }

    @staticmethod
    def _tx_result_without_state(tx_result: dict) -> dict:
        normalized = stringify_decimals(tx_result)
        return {
            key: value
            for key, value in normalized.items()
            if key not in {"state", "rewards", "reward_records"}
        }

    @staticmethod
    def _aggregate_reward_records(results: list[dict]) -> dict:
        totals: defaultdict[tuple[str, ...], decimal.Decimal] = defaultdict(
            lambda: decimal.Decimal("0")
        )
        for result in results:
            for record in result["tx_result"].get("reward_records") or []:
                normalized = stringify_decimals(record)
                key = (
                    normalized["type"],
                    normalized["recipient_key"],
                    normalized.get("source_contract"),
                    normalized.get("validator_key"),
                    normalized.get("delegator_key"),
                )
                totals[key] += decimal.Decimal(str(normalized["value"]))

        return dict(totals)

    def test_parallel_executor_matches_serial_execution(self):
        txs = [
            self._tx(
                sender="sys",
                contract="con_token_a",
                function="change_metadata",
                kwargs={"key": "alpha", "value": "one"},
                nonce=0,
                signature="sig-1",
            ),
            self._tx(
                sender="bob",
                contract="con_token_b",
                function="change_metadata",
                kwargs={"key": "beta", "value": "two"},
                nonce=0,
                signature="sig-2",
            ),
            self._tx(
                sender="sys",
                contract="con_token_a",
                function="change_metadata",
                kwargs={"key": "alpha", "value": "three"},
                nonce=1,
                signature="sig-3",
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as serial_dir,
            tempfile.TemporaryDirectory() as parallel_dir,
        ):
            serial_client, serial_processor = self._build_processor(
                Path(serial_dir) / "xian"
            )
            parallel_client, parallel_processor = self._build_processor(
                Path(parallel_dir) / "xian"
            )

            serial_results = [
                serial_processor.process_tx(
                    deepcopy(tx),
                    enabled_fees=False,
                    rewards_handler=None,
                    track_access=True,
                )
                for tx in txs
            ]

            executor = ParallelBlockExecutor(
                storage_home=Path(parallel_dir) / "xian",
                enabled=True,
                workers=1,
                min_transactions=1,
            )
            parallel_results, stats = executor.execute(
                txs=deepcopy(txs),
                tx_processor=parallel_processor,
                enabled_fees=False,
                rewards_handler=None,
            )

            self.assertEqual(stats.speculative_accepted, 2)
            self.assertEqual(stats.serial_fallbacks, 1)

            serial_tx_results = [
                self._tx_result_without_state(result["tx_result"])
                for result in serial_results
            ]
            parallel_tx_results = [
                self._tx_result_without_state(result["tx_result"])
                for result in parallel_results
            ]
            self.assertEqual(parallel_tx_results, serial_tx_results)

            for key in (
                "con_token_a.metadata:alpha",
                "con_token_b.metadata:beta",
            ):
                self.assertEqual(
                    parallel_client.raw_driver.get(key),
                    serial_client.raw_driver.get(key),
                )

    def test_parallel_executor_allows_additive_reward_overlap(self):
        txs = [
            self._tx(
                sender="sys",
                contract="con_token_a",
                function="change_metadata",
                kwargs={"key": "alpha", "value": "one"},
                nonce=0,
                signature="sig-r1",
            ),
            self._tx(
                sender="bob",
                contract="con_token_b",
                function="change_metadata",
                kwargs={"key": "beta", "value": "two"},
                nonce=0,
                signature="sig-r2",
            ),
            self._tx(
                sender="carol",
                contract="con_token_c",
                function="change_metadata",
                kwargs={"key": "gamma", "value": "three"},
                nonce=0,
                signature="sig-r3",
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as serial_dir,
            tempfile.TemporaryDirectory() as parallel_dir,
        ):
            serial_client, serial_processor = self._build_processor(
                Path(serial_dir) / "xian",
                with_rewards=True,
            )
            parallel_client, parallel_processor = self._build_processor(
                Path(parallel_dir) / "xian",
                with_rewards=True,
            )
            serial_rewards = RewardsHandler(client=serial_client)
            parallel_rewards = RewardsHandler(client=parallel_client)

            serial_results = [
                serial_processor.process_tx(
                    deepcopy(tx),
                    enabled_fees=True,
                    rewards_handler=serial_rewards,
                    track_access=True,
                )
                for tx in txs
            ]

            executor = ParallelBlockExecutor(
                storage_home=Path(parallel_dir) / "xian",
                enabled=True,
                workers=1,
                min_transactions=1,
            )
            parallel_results, stats = executor.execute(
                txs=deepcopy(txs),
                tx_processor=parallel_processor,
                enabled_fees=True,
                rewards_handler=parallel_rewards,
            )

            self.assertEqual(stats.speculative_accepted, 3)
            self.assertEqual(stats.serial_fallbacks, 0)

            serial_tx_results = [
                self._tx_result_without_state(result["tx_result"])
                for result in serial_results
            ]
            parallel_tx_results = [
                self._tx_result_without_state(result["tx_result"])
                for result in parallel_results
            ]
            self.assertEqual(parallel_tx_results, serial_tx_results)
            # Additive reward writes are rematerialized at commit time, so the
            # stable invariant is the aggregate reward allocation plus the final
            # post-block state, not the exact per-tx state snapshot.
            self.assertEqual(
                self._aggregate_reward_records(parallel_results),
                self._aggregate_reward_records(serial_results),
            )

            for key in (
                "currency.balances:foundation",
                "currency.balances:mn-1",
                "currency.balances:mn-2",
                "con_token_a.metadata:alpha",
                "con_token_b.metadata:beta",
                "con_token_c.metadata:gamma",
            ):
                self.assertEqual(
                    parallel_client.raw_driver.get(key),
                    serial_client.raw_driver.get(key),
                )

    def test_parallel_executor_falls_back_for_prefix_scan_reads(self):
        contract_code = textwrap.dedent(
            """
            values = Hash(default_value=0)
            out = Variable()

            @construct
            def seed():
                values['a'] = 1
                out.set(0)

            @export
            def add_value(key: str, amount: int):
                values[key] = amount

            @export
            def snapshot_sum():
                out.set(sum(values.all()))
                return out.get()
            """
        )

        txs = [
            self._tx(
                sender="alice",
                contract="con_scan",
                function="add_value",
                kwargs={"key": "b", "amount": 5},
                nonce=0,
                signature="sig-scan-1",
            ),
            self._tx(
                sender="bob",
                contract="con_scan",
                function="snapshot_sum",
                kwargs={},
                nonce=0,
                signature="sig-scan-2",
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as serial_dir,
            tempfile.TemporaryDirectory() as parallel_dir,
        ):
            serial_client = ContractingClient(
                storage_home=Path(serial_dir) / "xian"
            )
            parallel_client = ContractingClient(
                storage_home=Path(parallel_dir) / "xian"
            )
            serial_client.submit(contract_code, name="con_scan", signer="sys")
            parallel_client.submit(contract_code, name="con_scan", signer="sys")
            serial_client.raw_driver.commit()
            parallel_client.raw_driver.commit()

            serial_processor = TxProcessor(client=serial_client)
            parallel_processor = TxProcessor(client=parallel_client)

            serial_results = [
                serial_processor.process_tx(
                    deepcopy(tx),
                    enabled_fees=False,
                    rewards_handler=None,
                    track_access=True,
                )
                for tx in txs
            ]

            executor = ParallelBlockExecutor(
                storage_home=Path(parallel_dir) / "xian",
                enabled=True,
                workers=1,
                min_transactions=1,
            )
            parallel_results, stats = executor.execute(
                txs=deepcopy(txs),
                tx_processor=parallel_processor,
                enabled_fees=False,
                rewards_handler=None,
            )

            self.assertEqual(stats.speculative_accepted, 1)
            self.assertEqual(stats.serial_fallbacks, 1)
            self.assertEqual(
                serial_results[1]["access"].prefix_reads,
                frozenset({"con_scan.values:"}),
            )
            self.assertEqual(
                parallel_results[1]["access"].prefix_reads,
                frozenset({"con_scan.values:"}),
            )
            self.assertEqual(
                parallel_client.raw_driver.get("con_scan.out"),
                serial_client.raw_driver.get("con_scan.out"),
            )

    def test_process_tx_without_reward_config_keeps_rewards_optional(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client, tx_processor = self._build_processor(
                Path(temp_dir) / "xian"
            )

            result = tx_processor.process_tx(
                self._tx(
                    sender="sys",
                    contract="con_token_a",
                    function="change_metadata",
                    kwargs={"key": "alpha", "value": "one"},
                    nonce=0,
                    signature="sig-missing-rewards",
                ),
                enabled_fees=False,
                rewards_handler=RewardsHandler(client=client),
            )

            self.assertEqual(result["tx_result"]["status"], 0)
            self.assertIsNone(result["tx_result"]["rewards"])

    def test_parallel_executor_reuses_process_pool_between_blocks(self):
        created_executors = []

        class FakeExecutor:
            def __init__(self, *args, **kwargs):
                self.map_calls = 0
                self.shutdown_calls = 0
                created_executors.append(self)

            def map(self, fn, tasks):
                self.map_calls += 1
                return [{"tx_result": {}, "access": None} for _ in tasks]

            def shutdown(self, wait=True, cancel_futures=False):
                self.shutdown_calls += 1

        executor = ParallelBlockExecutor(
            storage_home=Path("/tmp/xian-test"),
            enabled=True,
            workers=2,
            min_transactions=1,
        )

        with patch("xian.parallel_executor.ProcessPoolExecutor", FakeExecutor):
            first = executor._speculate_many(
                txs=[{"payload": {"sender": "alice"}}],
                enabled_fees=False,
                rewards_handler=None,
            )
            second = executor._speculate_many(
                txs=[{"payload": {"sender": "bob"}}],
                enabled_fees=False,
                rewards_handler=None,
            )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(len(created_executors), 1)
        self.assertEqual(created_executors[0].map_calls, 2)

        executor.close()
        self.assertEqual(created_executors[0].shutdown_calls, 1)

    def test_worker_runtime_reuses_client_and_processor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_home = str(Path(temp_dir) / "xian")
            _WORKER_RUNTIMES.clear()

            first = _get_worker_runtime(
                storage_home=storage_home,
                use_rewards_handler=False,
            )
            second = _get_worker_runtime(
                storage_home=storage_home,
                use_rewards_handler=False,
            )
            third = _get_worker_runtime(
                storage_home=storage_home,
                use_rewards_handler=True,
            )

            self.assertIs(first, second)
            self.assertIsNot(first, third)
            self.assertIsNone(first.rewards_handler)
            self.assertIsNotNone(third.rewards_handler)


if __name__ == "__main__":
    unittest.main()
