import unittest

from xian.exceptions import TransactionException
from xian.fee_policy import (
    DEFAULT_FREE_BLOCK_MAX_CHI,
    DEFAULT_FREE_TX_MAX_CHI,
    TxFeePolicy,
)


def _tx(chi_supplied: int):
    return {"payload": {"chi_supplied": chi_supplied}}


class TxFeePolicyTests(unittest.TestCase):
    def test_default_runtime_policy_is_paid_metered(self):
        policy = TxFeePolicy.from_runtime_config({})

        self.assertEqual(policy.mode, "paid_metered")
        self.assertTrue(policy.meter_execution)
        self.assertTrue(policy.charge_fees)
        self.assertTrue(policy.distribute_fee_rewards)
        self.assertTrue(policy.require_chi_balance)
        self.assertIsNone(policy.max_tx_chi)
        self.assertIsNone(policy.max_block_chi)

    def test_free_metered_policy_uses_default_resource_caps(self):
        policy = TxFeePolicy.from_runtime_config({"tx_fee_mode": "free_metered"})

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

    def test_legacy_disabled_fees_remains_unmetered(self):
        policy = TxFeePolicy.from_legacy_enabled_fees(False)

        self.assertEqual(policy.mode, "legacy_unmetered")
        self.assertFalse(policy.meter_execution)
        self.assertFalse(policy.charge_fees)
        self.assertFalse(policy.distribute_fee_rewards)


if __name__ == "__main__":
    unittest.main()
