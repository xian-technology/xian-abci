import unittest

from xian.exceptions import TransactionException
from xian.fee_policy import (
    DEFAULT_FREE_BLOCK_MAX_CHI,
    DEFAULT_FREE_TX_MAX_CHI,
    FEE_POLICY_KEY,
    TxFeePolicy,
    fee_policy_state,
    reject_node_local_fee_settings,
    resolve_chain_fee_policy,
)
from xian.state_root import is_consensus_state_key


def _tx(chi_supplied: int):
    return {"payload": {"chi_supplied": chi_supplied}}


class TxFeePolicyTests(unittest.TestCase):
    def test_default_runtime_policy_is_paid_metered(self):
        policy = TxFeePolicy.from_settings(None)

        self.assertEqual(policy.mode, "paid_metered")
        self.assertTrue(policy.meter_execution)
        self.assertTrue(policy.charge_fees)
        self.assertTrue(policy.distribute_fee_rewards)
        self.assertTrue(policy.require_chi_balance)
        self.assertIsNone(policy.max_tx_chi)
        self.assertIsNone(policy.max_block_chi)

    def test_free_metered_policy_uses_default_resource_caps(self):
        policy = TxFeePolicy.from_settings({"tx_fee_mode": "free_metered"})

        self.assertEqual(policy.mode, "free_metered")
        self.assertTrue(policy.meter_execution)
        self.assertFalse(policy.charge_fees)
        self.assertFalse(policy.distribute_fee_rewards)
        self.assertFalse(policy.require_chi_balance)
        self.assertEqual(policy.max_tx_chi, DEFAULT_FREE_TX_MAX_CHI)
        self.assertEqual(policy.max_block_chi, DEFAULT_FREE_BLOCK_MAX_CHI)

    def test_free_metered_policy_enforces_caps(self):
        policy = TxFeePolicy.free_metered(max_tx_chi=10, max_block_chi=20)

        policy.validate_tx(_tx(10))
        policy.validate_block_total(20)
        with self.assertRaisesRegex(TransactionException, "chi_supplied exceeds"):
            policy.validate_tx(_tx(11))
        with self.assertRaisesRegex(TransactionException, "Block chi_supplied exceeds"):
            policy.validate_block_total(21)

    def test_metered_policies_reject_zero_chi_supplied(self):
        for policy in (TxFeePolicy.paid_metered(), TxFeePolicy.free_metered()):
            policy.validate_tx(_tx(1))
            for chi_supplied in (0, None):
                with self.assertRaisesRegex(TransactionException, "greater than zero"):
                    policy.validate_tx(_tx(chi_supplied))

    def test_system_policy_is_unmetered(self):
        policy = TxFeePolicy.system_unmetered()

        self.assertEqual(policy.mode, "system_unmetered")
        self.assertFalse(policy.meter_execution)
        self.assertFalse(policy.charge_fees)
        self.assertFalse(policy.distribute_fee_rewards)


    def test_genesis_fee_policy_state_is_canonical_and_round_trips(self):
        # The fee policy is consensus state: one canonical value per policy.
        self.assertEqual(fee_policy_state(None), {"tx_fee_mode": "paid_metered"})
        # Free limits are irrelevant to paid mode and are not stored.
        self.assertEqual(
            fee_policy_state({"tx_fee_mode": "paid_metered", "free_tx_max_chi": 5}),
            {"tx_fee_mode": "paid_metered"},
        )
        free = fee_policy_state(
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 10, "free_block_max_chi": 20}
        )
        self.assertEqual(
            free,
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 10, "free_block_max_chi": 20},
        )
        self.assertEqual(
            fee_policy_state({"tx_fee_mode": "free_metered"})["free_block_max_chi"],
            DEFAULT_FREE_BLOCK_MAX_CHI,
        )
        policy = TxFeePolicy.from_state(free)
        self.assertEqual((policy.max_tx_chi, policy.max_block_chi), (10, 20))
        self.assertEqual(policy.to_state(), free)
        self.assertTrue(is_consensus_state_key(FEE_POLICY_KEY))

    def test_invalid_fee_policy_inputs_and_state_are_rejected(self):
        for settings in (
            {"tx_fee_mode": "free"},
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 0},
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": True},
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": "10"},
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 30, "free_block_max_chi": 20},
            {"tx_fee_mode": "paid_metered", "unknown": 1},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                fee_policy_state(settings)
        for value in (
            None,
            "paid_metered",
            {"tx_fee_mode": "paid_metered", "free_tx_max_chi": 1},
            {"tx_fee_mode": "free_metered"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TxFeePolicy.from_state(value)

    def test_node_config_cannot_set_the_consensus_fee_policy(self):
        reject_node_local_fee_settings({"app_log_level": "INFO"})
        for key in ("tx_fee_mode", "free_tx_max_chi", "free_block_max_chi"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "genesis"):
                    reject_node_local_fee_settings({key: "paid_metered"})


    def test_smallest_valid_limits_are_accepted(self):
        state = fee_policy_state(
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 1, "free_block_max_chi": 1}
        )
        self.assertEqual((state["free_tx_max_chi"], state["free_block_max_chi"]), (1, 1))

    def test_chain_fee_policy_comes_from_committed_state_then_genesis(self):
        free = fee_policy_state({"tx_fee_mode": "free_metered"})
        genesis = {
            "abci_genesis": {
                "genesis": [
                    {"key": "con_x.value", "value": 1},
                    {"key": FEE_POLICY_KEY, "value": free},
                ]
            }
        }
        paid = {"tx_fee_mode": "paid_metered"}
        self.assertEqual(
            resolve_chain_fee_policy(committed_value=paid, genesis=genesis).mode, "paid_metered"
        )
        self.assertEqual(
            resolve_chain_fee_policy(committed_value=None, genesis=genesis).mode, "free_metered"
        )
        self.assertEqual(
            resolve_chain_fee_policy(
                committed_value=None, genesis=genesis["abci_genesis"]
            ).mode,
            "free_metered",
        )
        duplicate = {"genesis": genesis["abci_genesis"]["genesis"] * 2}
        for broken in (None, {}, {"abci_genesis": {"genesis": []}}, duplicate):
            with self.subTest(genesis=broken), self.assertRaisesRegex(ValueError, "exactly one"):
                resolve_chain_fee_policy(committed_value=None, genesis=broken)


if __name__ == "__main__":
    unittest.main()
