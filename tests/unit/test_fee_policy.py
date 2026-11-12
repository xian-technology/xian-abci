import unittest
from unittest import mock

from xian.exceptions import TransactionException
from xian.fee_policy import (
    DEFAULT_MAX_BLOCK_CHI,
    DEFAULT_MAX_TX_CHI,
    FEE_POLICY_KEY,
    MAX_BLOCK_CHI_LIMIT,
    TxFeePolicy,
    fee_policy_state,
    reject_node_local_fee_settings,
    resolve_chain_fee_policy,
)
from xian.state_root import is_consensus_state_key


def _tx(chi_supplied: int):
    return {"payload": {"chi_supplied": chi_supplied}}


def _state(mode: str, max_tx_chi: int, max_block_chi: int) -> dict:
    return {"tx_fee_mode": mode, "max_tx_chi": max_tx_chi, "max_block_chi": max_block_chi}


class TxFeePolicyTests(unittest.TestCase):
    def test_default_runtime_policy_is_paid_metered_with_hard_caps(self):
        policy = TxFeePolicy.from_settings(None)

        self.assertEqual(policy.mode, "paid_metered")
        self.assertTrue(policy.meter_execution)
        self.assertTrue(policy.charge_fees)
        self.assertTrue(policy.distribute_fee_rewards)
        self.assertTrue(policy.require_chi_balance)
        self.assertEqual(
            (policy.max_tx_chi, policy.max_block_chi), (DEFAULT_MAX_TX_CHI, DEFAULT_MAX_BLOCK_CHI)
        )
        self.assertEqual((DEFAULT_MAX_TX_CHI, DEFAULT_MAX_BLOCK_CHI), (200_000, 500_000))

    def test_free_metered_policy_uses_the_same_default_caps(self):
        policy = TxFeePolicy.from_settings({"tx_fee_mode": "free_metered"})

        self.assertEqual(policy.mode, "free_metered")
        self.assertTrue(policy.meter_execution)
        self.assertFalse(policy.charge_fees)
        self.assertFalse(policy.distribute_fee_rewards)
        self.assertFalse(policy.require_chi_balance)
        self.assertEqual(
            (policy.max_tx_chi, policy.max_block_chi), (DEFAULT_MAX_TX_CHI, DEFAULT_MAX_BLOCK_CHI)
        )

    def test_every_chain_fee_mode_enforces_its_caps(self):
        for factory in (TxFeePolicy.paid_metered, TxFeePolicy.free_metered):
            with self.subTest(mode=factory.__name__):
                policy = factory(max_tx_chi=10, max_block_chi=20)

                policy.validate_tx(_tx(10))
                policy.validate_block_total(20)
                with self.assertRaisesRegex(
                    TransactionException,
                    "chi_supplied 11 exceeds the chain's per-transaction limit of 10 chi",
                ):
                    policy.validate_tx(_tx(11))
                with self.assertRaisesRegex(
                    TransactionException,
                    "chi_supplied 21 would exceed the chain's per-block limit of 20 chi",
                ):
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
        self.assertFalse(policy.require_chi_balance)
        self.assertEqual((policy.max_tx_chi, policy.max_block_chi), (None, None))
        # System work has no budget and no caps, and is never chain state.
        policy.validate_tx(_tx(0))
        policy.validate_block_total(10**12)
        with self.assertRaisesRegex(ValueError, "not a chain fee policy"):
            policy.to_state()

    def test_genesis_fee_policy_state_is_canonical_and_round_trips(self):
        # The fee policy is consensus state: one canonical value per policy,
        # and every chain fee mode stores its caps explicitly.
        self.assertEqual(
            fee_policy_state(None),
            _state("paid_metered", DEFAULT_MAX_TX_CHI, DEFAULT_MAX_BLOCK_CHI),
        )
        for mode in ("paid_metered", "free_metered"):
            with self.subTest(mode=mode):
                state = fee_policy_state(_state(mode, 10, 20))
                self.assertEqual(state, _state(mode, 10, 20))
                policy = TxFeePolicy.from_state(state)
                self.assertEqual(
                    (policy.mode, policy.max_tx_chi, policy.max_block_chi), (mode, 10, 20)
                )
                self.assertEqual(policy.to_state(), state)
        self.assertTrue(is_consensus_state_key(FEE_POLICY_KEY))

    def test_invalid_fee_policy_inputs_and_state_are_rejected(self):
        for settings in (
            {"tx_fee_mode": "free"},
            {"tx_fee_mode": "free_metered", "max_tx_chi": 0},
            {"tx_fee_mode": "paid_metered", "max_tx_chi": True},
            {"tx_fee_mode": "paid_metered", "max_tx_chi": "10"},
            {"tx_fee_mode": "paid_metered", "max_block_chi": -1},
            _state("paid_metered", 30, 20),
            _state("free_metered", 30, 20),
            _state("paid_metered", 10, MAX_BLOCK_CHI_LIMIT + 1),
            {"tx_fee_mode": "paid_metered", "unknown": 1},
            # The pre-revision-11 free-mode-only field names are gone.
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 10},
            {"tx_fee_mode": "free_metered", "free_block_max_chi": 20},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                fee_policy_state(settings)
        for value in (
            None,
            "paid_metered",
            {"tx_fee_mode": "paid_metered"},
            {"tx_fee_mode": "free_metered"},
            {"tx_fee_mode": "paid_metered", "max_tx_chi": 10},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TxFeePolicy.from_state(value)

    def test_direct_construction_cannot_build_a_policy_without_valid_caps(self):
        for caps in ((0, 10), (10, 0), (30, 20), (10, MAX_BLOCK_CHI_LIMIT + 1), (None, None)):
            for factory in (TxFeePolicy.paid_metered, TxFeePolicy.free_metered):
                with self.subTest(caps=caps, mode=factory.__name__):
                    with self.assertRaises(ValueError):
                        factory(max_tx_chi=caps[0], max_block_chi=caps[1])

    def test_block_cap_upper_bound_is_inclusive(self):
        policy = TxFeePolicy.paid_metered(max_tx_chi=1, max_block_chi=MAX_BLOCK_CHI_LIMIT)
        self.assertEqual(policy.max_block_chi, MAX_BLOCK_CHI_LIMIT)

    def test_tx_cap_must_not_exceed_the_vm_ceiling(self):
        with mock.patch("xian.fee_policy.vm_max_tx_chi", return_value=100):
            TxFeePolicy.paid_metered(max_tx_chi=100, max_block_chi=200)
            for settings in (_state("paid_metered", 101, 200), _state("free_metered", 101, 200)):
                with self.subTest(settings=settings):
                    with self.assertRaisesRegex(ValueError, "VM ceiling of 100 chi"):
                        fee_policy_state(settings)
                    with self.assertRaisesRegex(ValueError, "VM ceiling of 100 chi"):
                        TxFeePolicy.from_state(settings)

    def test_node_config_cannot_set_the_consensus_fee_policy(self):
        reject_node_local_fee_settings({"app_log_level": "INFO"})
        for key in ("tx_fee_mode", "max_tx_chi", "max_block_chi"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "genesis"):
                    reject_node_local_fee_settings({key: "paid_metered"})

    def test_smallest_valid_limits_are_accepted(self):
        state = fee_policy_state(_state("free_metered", 1, 1))
        self.assertEqual((state["max_tx_chi"], state["max_block_chi"]), (1, 1))

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
        paid = fee_policy_state({"tx_fee_mode": "paid_metered"})
        self.assertEqual(
            resolve_chain_fee_policy(committed_value=paid, genesis=genesis).mode, "paid_metered"
        )
        self.assertEqual(
            resolve_chain_fee_policy(committed_value=None, genesis=genesis).mode, "free_metered"
        )
        self.assertEqual(
            resolve_chain_fee_policy(committed_value=None, genesis=genesis["abci_genesis"]).mode,
            "free_metered",
        )
        duplicate = {"genesis": genesis["abci_genesis"]["genesis"] * 2}
        for broken in (None, {}, {"abci_genesis": {"genesis": []}}, duplicate):
            with self.subTest(genesis=broken), self.assertRaisesRegex(ValueError, "exactly one"):
                resolve_chain_fee_policy(committed_value=None, genesis=broken)


if __name__ == "__main__":
    unittest.main()
