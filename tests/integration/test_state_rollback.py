import datetime
import unittest
from pathlib import Path

import pytest
from contracting.local import ContractingClient
from contracting.storage.driver import Driver
from xian_runtime_types.decimal import ContractingDecimal

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.tx import transaction_bytes


def process_tx(processor, tx, fee_policy=None):
    """The block executor's response for ``tx`` alone in a block (unmetered
    unless ``fee_policy`` says otherwise)."""
    [response] = processor.process_block(
        [tx], [transaction_bytes(tx)], fee_policy=fee_policy or TxFeePolicy.system_unmetered()
    )
    return response


def create_block_meta(dt: datetime.datetime = datetime.datetime.now()):
    # Get the current time in nanoseconds
    nanos = int(dt.timestamp() * 1e9)
    # Mock b_meta dictionary with current nanoseconds
    return {
        "nanos": nanos,  # Current nanoseconds timestamp
        "height": 123456,  # Example block number
        "chain_id": "test-chain",  # Example chain ID
        "hash": "abc123def456",  # Example block hash
    }


@pytest.mark.usefixtures("node_fixture")
class TestStateRollback(unittest.TestCase):
    def setUp(self):
        self.driver = Driver(vm_profile="xian_vm_v2", storage_home=self.constants.STORAGE_HOME)
        self.addCleanup(self.driver.close)
        self.client = ContractingClient(vm_profile="xian_vm_v2", driver=self.driver)
        self.addCleanup(self.client.close)
        self.client.flush()
        self.txp = TxProcessor(client=self.client)
        base_dir = Path(__file__).resolve().parent / "contracts/rollback"
        with open(f"{base_dir}/con_mutable_map.py") as f:
            code = f.read()
        self.client.submit(code, name="con_mutable_map")
        self.contract = self.client.get_contract_proxy("con_mutable_map")
        # Submit cross-contract mutator
        with open(f"{base_dir}/con_mutator.py") as f:
            code = f.read()
        self.client.submit(code, name="con_mutator")
        # Submit token contract to use for overdraw failure
        with open(f"{base_dir}/currency_1.py") as f:
            code = f.read()
        self.client.submit(code, name="currency_1")
        # Submit a failer that mutates then attempts overdraw via token
        with open(f"{base_dir}/con_overdraw_fail.py") as f:
            code = f.read()
        self.client.submit(code, name="con_overdraw_fail")
        # Add callee-side failure to mutator
        with open(f"{base_dir}/con_mutator_fail.py") as f:
            code = f.read()
        self.client.submit(code, name="con_mutator_fail")
        # Persist submissions to disk and clear caches so no writes are pending
        self.driver.commit()

    def tearDown(self):
        self.client.flush()

    def assert_no_driver_writes(self, where: str = ""):
        self.assertEqual(self.driver.pending_items(), [], f"pending writes not empty {where}")
        self.assertEqual(self.driver.transaction_writes, {}, f"transaction_writes not empty {where}")

    def commit_block(self, b_meta):
        # Mirror block boundary commit behavior
        self.driver.commit()

    def driver_read_nested(self, key: str):
        # Directly query the driver for on-disk/cached state
        return self.driver.get(self.driver.make_key("con_mutable_map", "nested", [key]))

    def assert_no_contract_writes(self, where: str = ""):
        leaked = [k for k in dict(self.driver.pending_items()) if k.startswith("con_mutable_map.")]
        self.assertEqual(leaked, [], f"unexpected con_mutable_map writes {where}: {leaked}")

    def assert_currency_write_present(self, sender: str = "alice", where: str = ""):
        keys = list(dict(self.driver.pending_items()))
        self.assertTrue(any(k.startswith(f"currency.balances:{sender}") for k in keys), f"expected chi write missing {where}: {keys}")

    def assert_contract_keys_equal(self, expected_keys, where: str = ""):
        actual = sorted([k for k in dict(self.driver.pending_items()) if k.startswith("con_mutable_map.")])
        self.assertEqual(actual, sorted(expected_keys), f"unexpected con_mutable_map writes {where}: {actual}")

    def test_in_place_mutation_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = create_block_meta(datetime.datetime.now())

        # Tx 1: initialize (expected success)
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )

        # Sanity check after tx1
        self.assertEqual(self.contract.nested["k"]["count"], 1)
        self.assertEqual(self.contract.nested["k"]["items"], [1])
        # Commit end of block and verify driver state
        self.commit_block(bm1)
        self.assertEqual(self.driver_read_nested("k"), {"count": 1, "items": [1]})
        self.assert_no_contract_writes("after commit bm1")

        # Tx 2: mutate in-place then fail
        res2 = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_in_place_and_fail",
                    "kwargs": {"key": "k"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )

        # Ensure the tx failed as intended
        self.assertNotEqual(res2.get("tx_result", {}).get("status"), 0)
        # Failure should not introduce new contract writes
        self.assert_no_contract_writes("after mutate_in_place_and_fail failure")
        # Expected behavior: rollback => state unchanged
        self.assertEqual(self.contract.nested["k"]["count"], 1)
        self.assertEqual(self.contract.nested["k"]["items"], [1])

    def test_same_block_in_place_mutation_rollback(self):
        bm = create_block_meta(datetime.datetime.now())

        # Tx 1: initialize in same block
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_sb"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm,
            }
        )

        # Tx 2: same block, mutate then fail
        # Snapshot current contract writes before failure
        pre_keys = sorted([k for k in dict(self.driver.pending_items()) if k.startswith("con_mutable_map.")])
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_in_place_and_fail",
                    "kwargs": {"key": "k_sb"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm,
            }
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        post_keys = sorted([k for k in dict(self.driver.pending_items()) if k.startswith("con_mutable_map.")])
        self.assertEqual(pre_keys, post_keys, "same-block failure introduced unexpected contract writes")
        # In same block, the init write remains pending; assert no extra contract writes leaked
        self.assert_contract_keys_equal(["con_mutable_map.nested:k_sb"], "after same-block mutate + fail")
        self.assertEqual(self.contract.nested["k_sb"]["count"], 1)
        self.assertEqual(self.contract.nested["k_sb"]["items"], [1])

    def test_nested_mutation_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = create_block_meta(datetime.datetime.now())

        # init nested structure
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_nested",
                    "kwargs": {"key": "k_nested"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_nested"]["child"]["x"], 1)
        self.assertEqual(self.contract.nested["k_nested"]["items"], [{"v": 1}])
        # Commit block so no pending writes carry into the failing tx
        self.commit_block(bm1)
        self.assert_no_contract_writes("after commit bm1 nested")

        # mutate deep and fail
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_nested_and_fail",
                    "kwargs": {"key": "k_nested"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        self.assert_no_contract_writes("after mutate_nested_and_fail failure")
        self.assertEqual(self.contract.nested["k_nested"]["child"]["x"], 1)
        self.assertEqual(self.contract.nested["k_nested"]["items"], [{"v": 1}])

    def test_reassign_after_mutation_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())

        # init simple
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_reassign"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_reassign"]["count"], 1)
        self.assertEqual(self.contract.nested["k_reassign"]["items"], [1])

    def test_aliasing_default_mutation_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = create_block_meta(datetime.datetime.now())

        # two different keys use default
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "alias_set",
                    "kwargs": {"key": "a1"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        # alias_set writes should be pending until commit
        self.assert_contract_keys_equal(["con_mutable_map.aliased:a1"], "after alias_set a1 success")
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "alias_set",
                    "kwargs": {"key": "a2"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assert_contract_keys_equal(["con_mutable_map.aliased:a1", "con_mutable_map.aliased:a2"], "after alias_set a2 success")

        # mutate a1 then fail; ensure a2 unchanged and a1 rolled back
        pre_keys = sorted([k for k in dict(self.driver.pending_items()) if k.startswith("con_mutable_map.")])
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "alias_mutate_and_fail",
                    "kwargs": {"key": "a1"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        post_keys = sorted([k for k in dict(self.driver.pending_items()) if k.startswith("con_mutable_map.")])
        self.assertEqual(pre_keys, post_keys, "alias failure introduced unexpected contract writes")
        self.assertEqual(self.contract.aliased["a1"], {"count": 0, "items": []})
        self.assertEqual(self.contract.aliased["a2"], {"count": 0, "items": []})

    def test_callee_failure_after_cross_mutation_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = create_block_meta(datetime.datetime.now())

        # init simple
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_callee"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_callee"]["count"], 1)
        self.assertEqual(self.contract.nested["k_callee"]["items"], [1])

        # call mutator that fails inside callee
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "cross_mutate_then_fail",
                    "kwargs": {"key": "k_callee", "mutator": "con_mutator_fail"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        self.assertEqual(self.contract.nested["k_callee"]["count"], 1)
        self.assertEqual(self.contract.nested["k_callee"]["items"], [1])

    def test_multiple_failing_txs_same_and_next_block(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = bm1  # same block
        bm3 = create_block_meta(datetime.datetime.now())  # next block

        # init
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_multi"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_multi"]["count"], 1)
        self.assertEqual(self.contract.nested["k_multi"]["items"], [1])

        # two failing tx in same block
        res1 = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_in_place_and_fail",
                    "kwargs": {"key": "k_multi"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        res2 = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_then_reassign_and_fail",
                    "kwargs": {"key": "k_multi"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        self.assertNotEqual(res1.get("tx_result", {}).get("status"), 0)
        self.assertNotEqual(res2.get("tx_result", {}).get("status"), 0)
        # init write remains pending in same block; ensure no new contract writes
        self.assert_contract_keys_equal(["con_mutable_map.nested:k_multi"], "after same-block dual failures")
        self.assertEqual(self.contract.nested["k_multi"]["count"], 1)
        self.assertEqual(self.contract.nested["k_multi"]["items"], [1])

        # Commit end of this block before next-block failure
        self.commit_block(bm2)
        self.assert_no_contract_writes("after commit bm2 before next block")

        # failing tx in next block
        res3 = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_in_place_and_fail",
                    "kwargs": {"key": "k_multi"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm3,
            }
        )
        self.assertNotEqual(res3.get("tx_result", {}).get("status"), 0)
        self.assert_no_contract_writes("after next-block failure")
        self.assertEqual(self.contract.nested["k_multi"]["count"], 1)
        self.assertEqual(self.contract.nested["k_multi"]["items"], [1])

    def test_type_error_failure_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())

        # init simple
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_type"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_type"]["count"], 1)
        self.assertEqual(self.contract.nested["k_type"]["items"], [1])

    def test_cross_contract_token_overdraw_failure_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = create_block_meta(datetime.datetime.now())

        # init simple
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_tok"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_tok"]["count"], 1)
        self.assertEqual(self.contract.nested["k_tok"]["items"], [1])
        # Commit before failing tx
        self.commit_block(bm1)
        self.assert_no_contract_writes("after commit bm1 overdraw")

        # call failer that mutates then calls token transfer, causing overdraw failure
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_overdraw_fail",
                    "function": "mutate_then_overdraw",
                    "kwargs": {"key": "k_tok", "token": "currency_1", "to": "bob", "amount": 1},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        self.assert_no_contract_writes("after mutate_then_overdraw failure")
        # State should be unchanged due to rollback
        val = self.contract.nested["k_tok"]
        self.assertIsNotNone(val)
        self.assertEqual(val, {"count": 1, "items": [1]})

        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "type_error_after_mutation",
                    "kwargs": {"key": "k_type"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        self.assert_no_contract_writes("after type_error_after_mutation failure")
        self.assertEqual(self.contract.nested["k_type"], None)

    def test_cross_contract_mutation_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = create_block_meta(datetime.datetime.now())

        # init simple
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_cross"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_cross"]["count"], 1)
        self.assertEqual(self.contract.nested["k_cross"]["items"], [1])

        # call cross mutation, then fail in caller
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "cross_mutate_then_fail",
                    "kwargs": {"key": "k_cross", "mutator": "con_mutator"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            }
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        # Expected unchanged
        self.assertEqual(self.contract.nested["k_cross"]["count"], 1)
        self.assertEqual(self.contract.nested["k_cross"]["items"], [1])

    def test_fees_enabled_failing_tx_rollback(self):
        bm1 = create_block_meta(datetime.datetime.now())
        bm2 = create_block_meta(datetime.datetime.now())
        # Cover the submitted fee budget so execution reaches the deliberate
        # post-mutation failure rather than rejecting an unfunded transaction.
        self.driver.set("currency.balances:alice", 1000)
        self.driver.set("chi_cost.S:value", 100)

        # init simple
        process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "init_key",
                    "kwargs": {"key": "k_fees"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm1,
            }
        )
        self.assertEqual(self.contract.nested["k_fees"]["count"], 1)
        self.assertEqual(self.contract.nested["k_fees"]["items"], [1])
        # Commit before failing tx
        self.commit_block(bm1)
        self.assert_no_contract_writes("after commit bm1 fees")

        # failing tx with paid metering
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_in_place_and_fail",
                    "kwargs": {"key": "k_fees"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            },
            fee_policy=TxFeePolicy.paid_metered(),
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        self.assertIn("Forced failure after in-place mutation", res["tx_result"]["result"])
        self.assertGreater(res["tx_result"]["chi_used"], 0)
        balance_after_first = 1000 - ContractingDecimal(res["tx_result"]["chi_used"]) / 100
        self.assertEqual(self.driver.get("currency.balances:alice"), balance_after_first)
        # Fees enabled: expect only currency chi write; no contract writes
        self.assert_currency_write_present(where="after fees-enabled mutate_in_place_and_fail failure")
        self.assert_no_contract_writes("after fees-enabled mutate_in_place_and_fail failure")
        self.assertEqual(self.contract.nested["k_fees"]["count"], 1)
        self.assertEqual(self.contract.nested["k_fees"]["items"], [1])

        # mutate then reassign and fail (fees enabled)
        res = process_tx(
            self.txp,
            tx={
                "payload": {
                    "sender": "alice",
                    "contract": "con_mutable_map",
                    "function": "mutate_then_reassign_and_fail",
                    "kwargs": {"key": "k_fees"},
                    "chi_supplied": 1000,
                },
                "metadata": {"signature": "abc"},
                "b_meta": bm2,
            },
            fee_policy=TxFeePolicy.paid_metered(),
        )
        self.assertNotEqual(res.get("tx_result", {}).get("status"), 0)
        self.assertIn("Forced failure after reassign", res["tx_result"]["result"])
        self.assertGreater(res["tx_result"]["chi_used"], 0)
        self.assertEqual(
            self.driver.get("currency.balances:alice"),
            balance_after_first - ContractingDecimal(res["tx_result"]["chi_used"]) / 100,
        )
        self.assert_currency_write_present(where="after fees-enabled mutate_then_reassign_and_fail failure")
        self.assert_no_contract_writes("after fees-enabled mutate_then_reassign_and_fail failure")
        self.assertEqual(self.contract.nested["k_fees"]["count"], 1)
        self.assertEqual(self.contract.nested["k_fees"]["items"], [1])


if __name__ == "__main__":
    unittest.main()
