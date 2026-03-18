import os
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from contracting.client import ContractingClient

from xian.parallel_executor import ParallelBlockExecutor
from xian.processor import TxProcessor
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
    def _build_processor(self, storage_home: Path):
        client = ContractingClient(storage_home=storage_home)
        tx_processor = TxProcessor(client=client)

        contract_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "contracts"
        token_code = (contract_dir / "token_contract.py").read_text(
            encoding="utf-8"
        )

        client.submit(token_code, name="con_token_a", signer="sys")
        client.submit(token_code, name="con_token_b", signer="bob")
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

        with tempfile.TemporaryDirectory() as serial_dir, tempfile.TemporaryDirectory() as parallel_dir:
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
                stringify_decimals(result["tx_result"]) for result in serial_results
            ]
            parallel_tx_results = [
                stringify_decimals(result["tx_result"])
                for result in parallel_results
            ]
            self.assertEqual(parallel_tx_results, serial_tx_results)

            for key in ("con_token_a.metadata:alpha", "con_token_b.metadata:beta"):
                self.assertEqual(
                    parallel_client.raw_driver.get(key),
                    serial_client.raw_driver.get(key),
                )


if __name__ == "__main__":
    unittest.main()
