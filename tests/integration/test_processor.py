import os
import time
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from contracting.local import ContractingClient
from xian_runtime_types.time import Datetime

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.block import nanoseconds_to_utc_datetime
from xian.utils.tx import transaction_bytes


def create_block_meta(dt: datetime = datetime.now()):
    # Get the current time in nanoseconds
    nanos = int(time.mktime(dt.timetuple()) * 1e9 + dt.microsecond * 1e3)
    # Mock b_meta dictionary with current nanoseconds
    return {
        "nanos": nanos,  # Current nanoseconds timestamp
        "height": 123456,  # Example block number
        "chain_id": "test-chain",  # Example chain ID
        "hash": "abc123def456",  # Example block hash
    }


@pytest.mark.usefixtures("node_fixture")
class TestProcessor(unittest.TestCase):
    def setUp(self):
        self.c = ContractingClient(
            vm_profile="xian_vm_v2", storage_home=self.constants.STORAGE_HOME
        )
        self.addCleanup(self.c.close)
        self.d = self.c.raw_driver
        self.c.flush()
        # Paid cases price chi at 1 per token (the rate genesis would seed).
        self.d.set("chi_cost.S:value", 1)
        self.tx_processor = TxProcessor(client=self.c)
        self.script_dir = os.path.dirname(os.path.abspath(__file__))

        token_path = os.path.join(self.script_dir, "contracts", "token_contract.py")
        with open(token_path) as f:
            code = f.read()
            self.c.submit(code, name="currency_1")

        self.currency_1 = self.c.get_contract_proxy("currency_1")

        proxy_path = os.path.join(self.script_dir, "contracts", "proxy.py")
        with open(proxy_path) as f:
            code = f.read()
            self.c.submit(code, name="proxy")

        self.proxy = self.c.get_contract_proxy("proxy")

    def _process(self, tx, fee_policy):
        [response] = self.tx_processor.process_block(
            [tx], [transaction_bytes(tx)], fee_policy=fee_policy
        )
        return response

    def tearDown(self):
        # Called after every test, ensures each test starts with a clean slate and is isolated from others
        self.c.flush()

    def test_transfer_returns_event(self):
        # Setup - approve first
        self.d.set(
            key="currency_1.approvals:sys:bob",
            value=100000,
        )
        self.d.set(
            key="currency_1.balances:sys",
            value=100000,
        )
        # Now transfer
        res = self._process(
            {
                "payload": {
                    "contract": "currency_1",
                    "function": "transfer_from",
                    "sender": "bob",
                    "kwargs": {
                        "amount": 100,
                        "to": "bob",
                        "main_account": "sys",
                    },
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": create_block_meta(),
            },
            TxFeePolicy.system_unmetered(),
        )
        expected_events = [
            {
                "caller": "bob",
                "contract": "currency_1",
                "event": "Transfer",
                "data_indexed": {"from": "sys", "to": "bob"},
                "data": {"amount": 100},
                "signer": "bob",
            }
        ]

        self.assertEqual(res["tx_result"]["events"], expected_events)
        self.assertNotIn("transaction", res["tx_result"])

    def test_send_multiple_returns_events(self):
        self.d.set(
            key="currency_1.balances:proxy",
            value=100000,
        )
        self.d.set(
            key="currency_1.balances:sys",
            value=100000,
        )
        self.d.set(
            key="currency_1.balances:bob",
            value=100000,
        )
        self.d.set(
            key="currency.balances:bob",
            value=100000,
        )
        res = self._process(
            {
                "payload": {
                    "contract": "proxy",
                    "function": "send_multiple",
                    "sender": "bob",
                    "kwargs": {
                        "amount": 100,
                        "to": ["casey", "francis", "sally", "ed", "yolanda"],
                    },
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": create_block_meta(),
            },
            TxFeePolicy.paid_metered(),
        )

        expected_events = [
            {
                "caller": "proxy",
                "contract": "currency_1",
                "event": "Transfer",
                "data_indexed": {"from": "proxy", "to": "casey"},
                "data": {"amount": 100},
                "signer": "bob",
            },
            {
                "caller": "proxy",
                "contract": "currency_1",
                "event": "Transfer",
                "data_indexed": {"from": "proxy", "to": "francis"},
                "data": {"amount": 100},
                "signer": "bob",
            },
            {
                "caller": "proxy",
                "contract": "currency_1",
                "event": "Transfer",
                "data_indexed": {"from": "proxy", "to": "sally"},
                "data": {"amount": 100},
                "signer": "bob",
            },
            {
                "caller": "proxy",
                "contract": "currency_1",
                "event": "Transfer",
                "data_indexed": {"from": "proxy", "to": "ed"},
                "data": {"amount": 100},
                "signer": "bob",
            },
            {
                "caller": "proxy",
                "contract": "currency_1",
                "event": "Transfer",
                "data_indexed": {"from": "proxy", "to": "yolanda"},
                "data": {"amount": 100},
                "signer": "bob",
            },
        ]

        self.assertEqual(res["tx_result"]["events"], expected_events)

    def test_block_time_from_nanos_preserves_subsecond_precision(self):
        dt = datetime(2026, 3, 19, 12, 34, 56, 789123, tzinfo=UTC)
        nanos = int(dt.timestamp()) * 1_000_000_000 + 789_123_456

        now = Datetime._from_datetime(nanoseconds_to_utc_datetime(nanos))

        self.assertEqual(now.year, 2026)
        self.assertEqual(now.month, 3)
        self.assertEqual(now.day, 19)
        self.assertEqual(now.hour, 12)
        self.assertEqual(now.minute, 34)
        self.assertEqual(now.second, 56)
        self.assertEqual(now.microsecond, 789123)

    def test_process_block_meters_transaction_bytes(self):
        self.d.set("currency.balances:bob", 100000)
        self.d.set("currency_1.balances:bob", 100000)

        base_tx = {
            "payload": {
                "contract": "currency_1",
                "function": "transfer",
                "sender": "bob",
                "kwargs": {"amount": 5, "to": "casey"},
                "chi_supplied": 1000,
            },
            "metadata": {"signature": "a"},
            "b_meta": create_block_meta(),
        }
        larger_tx = {
            **base_tx,
            "metadata": {"signature": "a" * 5001},
        }

        base_result = self._process(
            base_tx,
            TxFeePolicy.paid_metered(),
        )
        larger_result = self._process(
            larger_tx,
            TxFeePolicy.paid_metered(),
        )

        self.assertGreater(
            larger_result["tx_result"]["chi_used"],
            base_result["tx_result"]["chi_used"],
        )

    def test_free_metered_execution_does_not_debit_native_balance(self):
        self.d.set("chi_cost.S:value", 20)
        self.d.set("currency.balances:bob", 100000)
        self.d.set("currency_1.balances:bob", 100000)

        result = self._process(
            {
                "payload": {
                    "contract": "currency_1",
                    "function": "transfer",
                    "sender": "bob",
                    "kwargs": {"amount": 5, "to": "casey"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": create_block_meta(),
            },
            TxFeePolicy.free_metered(
                max_tx_chi=1000,
                max_block_chi=1000,
            ),
        )

        self.assertEqual(result["tx_result"]["status"], 0)
        self.assertGreater(result["tx_result"]["chi_used"], 0)
        self.assertEqual(self.d.get("currency.balances:bob"), 100000)
        self.assertNotIn(
            "currency.balances:bob",
            {write["key"] for write in result["tx_result"]["state"]},
        )
        self.assertIsNone(result["tx_result"]["rewards"])

    def test_reset_block_cache_clears_verified_proof_cache(self):
        with patch("xian.processor.zk_bridge.clear_verified_proof_cache") as clear_cache:
            self.tx_processor.reset_block_cache()

        clear_cache.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
