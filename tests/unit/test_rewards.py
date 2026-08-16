import unittest
from decimal import Decimal

from xian.rewards import RewardsHandler


class _FakeClient:
    def __init__(
        self,
        developers=None,
        validators=None,
        validator_powers=None,
        reward_keys=None,
        commission_bps=None,
        self_bonds=None,
        delegations=None,
        delegator_reward_keys=None,
    ):
        developers = developers or {
            "con_parent": "alice",
            "con_child": "bob",
        }
        validators = validators or []
        validator_powers = validator_powers or {}
        reward_keys = reward_keys or {}
        commission_bps = commission_bps or {}
        self_bonds = self_bonds or {}
        delegations = delegations or {}
        delegator_reward_keys = delegator_reward_keys or {}
        self._values = {
            ("rewards", "S", ("value",)): [
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("1"),
            ],
            ("chi_cost", "S", ("value",)): Decimal("20"),
            ("foundation", "owner", ()): "foundation",
            ("validators", "active_validators", ()): validators,
        }
        for contract, developer in developers.items():
            self._values[(contract, "__developer__", ())] = developer
        for validator, power in validator_powers.items():
            self._values[("validators", "powers", (validator,))] = power
        for validator, reward_key in reward_keys.items():
            self._values[("validators", "reward_keys", (validator,))] = reward_key
        for validator, bps in commission_bps.items():
            self._values[("validators", "commission_bps", (validator,))] = bps
        for validator, amount in self_bonds.items():
            self._values[("validators", "self_bond", (validator,))] = amount
        delegator_lists: dict[str, list[str]] = {}
        for (delegator, validator), amount in delegations.items():
            self._values[
                ("validators", "delegations", (delegator, validator))
            ] = amount
            delegator_lists.setdefault(validator, []).append(delegator)
        for validator, delegators in delegator_lists.items():
            self._values[("validators", "delegator_lists", (validator,))] = delegators
        for (delegator, validator), reward_key in delegator_reward_keys.items():
            self._values[
                ("validators", "delegator_reward_keys", (delegator, validator))
            ] = reward_key

    def get_var(self, contract, variable, arguments=None):
        args = tuple(arguments or ())
        return self._values.get((contract, variable, args))


class RewardsHandlerTests(unittest.TestCase):
    def test_build_tx_reward_outputs_returns_safe_empty_triplet_when_config_incomplete(
        self,
    ):
        client = _FakeClient()
        client._values[("foundation", "owner", ())] = None
        handler = RewardsHandler(client=client)

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_parent",
        )

        self.assertIsNone(rewards)
        self.assertEqual(reward_deltas, {})
        self.assertEqual(reward_records, [])

    def test_build_tx_reward_outputs_splits_developer_rewards_by_contract_cost(
        self,
    ):
        handler = RewardsHandler(client=_FakeClient())

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_parent",
            contract_costs={
                "con_parent": 7000,
                "con_child": 3000,
            },
        )

        self.assertEqual(str(rewards["developer_reward"]["alice"]), "3.5")
        self.assertEqual(str(rewards["developer_reward"]["bob"]), "1.5")
        self.assertEqual(
            str(reward_deltas["currency.balances:alice"]),
            "3.5",
        )
        self.assertEqual(
            str(reward_deltas["currency.balances:bob"]),
            "1.5",
        )
        self.assertEqual(
            [
                {
                    "type": record["type"],
                    "recipient_key": record["recipient_key"],
                    "source_contract": record["source_contract"],
                    "value": str(record["value"]),
                }
                for record in reward_records
            ],
            [
                {
                    "type": "developer_reward",
                    "recipient_key": "bob",
                    "source_contract": "con_child",
                    "value": "1.5",
                },
                {
                    "type": "developer_reward",
                    "recipient_key": "alice",
                    "source_contract": "con_parent",
                    "value": "3.5",
                },
            ],
        )

    def test_build_tx_reward_outputs_aggregates_same_developer_across_contracts(
        self,
    ):
        handler = RewardsHandler(
            client=_FakeClient(
                developers={
                    "con_parent": "alice",
                    "con_child": "alice",
                }
            )
        )

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_parent",
            contract_costs={
                "con_parent": 7000,
                "con_child": 3000,
            },
        )

        self.assertEqual(str(rewards["developer_reward"]["alice"]), "5")
        self.assertEqual(str(reward_deltas["currency.balances:alice"]), "5")
        self.assertEqual(len(reward_records), 2)
        self.assertEqual(
            {record["source_contract"] for record in reward_records},
            {"con_parent", "con_child"},
        )

    def test_unclaimed_developer_rewards_return_to_validator_and_delegator_pool(self):
        client = _FakeClient(
            developers={"con_system": "sys"},
            validators=["node1"],
            validator_powers={"node1": Decimal("10")},
            reward_keys={"node1": "validator-reward"},
            self_bonds={"node1": Decimal("100")},
            delegations={("alice", "node1"): Decimal("100")},
            delegator_reward_keys={("alice", "node1"): "alice-reward"},
        )
        client._values[("rewards", "S", ("value",))] = [
            Decimal("0.70"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0.30"),
        ]
        handler = RewardsHandler(client=client)

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_system",
        )

        self.assertEqual(str(rewards["validator_reward"]["validator-reward"]), "2.5")
        self.assertEqual(str(rewards["delegator_reward"]["alice-reward"]), "2.5")
        self.assertEqual(rewards["developer_reward"], {})
        self.assertEqual(rewards["foundation_reward"], {})
        self.assertEqual(str(reward_deltas["currency.balances:validator-reward"]), "2.5")
        self.assertEqual(str(reward_deltas["currency.balances:alice-reward"]), "2.5")
        self.assertNotIn("currency.balances:foundation", reward_deltas)
        self.assertEqual(
            [record["type"] for record in reward_records],
            ["validator_reward", "delegator_reward"],
        )

    def test_only_unclaimed_developer_share_returns_to_validator_pool(self):
        client = _FakeClient(
            developers={
                "con_app": "alice",
                "con_system": "sys",
            },
            validators=["node1"],
            validator_powers={"node1": Decimal("10")},
            reward_keys={"node1": "validator-reward"},
        )
        client._values[("rewards", "S", ("value",))] = [
            Decimal("0.70"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0.30"),
        ]
        handler = RewardsHandler(client=client)

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_app",
            contract_costs={
                "con_app": 50,
                "con_system": 50,
            },
        )

        self.assertEqual(
            str(rewards["validator_reward"]["validator-reward"]),
            "4.25",
        )
        self.assertEqual(str(rewards["developer_reward"]["alice"]), "0.75")
        self.assertEqual(rewards["foundation_reward"], {})
        self.assertEqual(
            str(reward_deltas["currency.balances:validator-reward"]),
            "4.25",
        )
        self.assertEqual(str(reward_deltas["currency.balances:alice"]), "0.75")
        self.assertNotIn("currency.balances:foundation", reward_deltas)
        self.assertEqual(
            [record["type"] for record in reward_records],
            ["validator_reward", "developer_reward"],
        )
        self.assertEqual(
            [record.get("source_contract") for record in reward_records],
            [None, "con_app"],
        )

    def test_build_tx_reward_outputs_splits_validator_rewards_by_power(self):
        client = _FakeClient(
            validators=["node1", "node2"],
            validator_powers={
                "node1": Decimal("30"),
                "node2": Decimal("10"),
            },
            reward_keys={
                "node1": "validator-reward-1",
                "node2": "validator-reward-2",
            },
        )
        client._values[("rewards", "S", ("value",))] = [
            Decimal("0.40"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0.60"),
        ]
        handler = RewardsHandler(client=client)

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_parent",
        )

        self.assertEqual(
            str(rewards["validator_reward"]["validator-reward-1"]), "1.5"
        )
        self.assertEqual(
            str(rewards["validator_reward"]["validator-reward-2"]), "0.5"
        )
        self.assertEqual(
            str(reward_deltas["currency.balances:validator-reward-1"]), "1.5"
        )
        self.assertEqual(
            str(reward_deltas["currency.balances:validator-reward-2"]), "0.5"
        )
        self.assertEqual(
            [record["recipient_key"] for record in reward_records if record["type"] == "validator_reward"],
            ["validator-reward-1", "validator-reward-2"],
        )

    def test_build_tx_reward_outputs_falls_back_to_equal_validator_weights(self):
        client = _FakeClient(
            validators=["node1", "node2"],
        )
        client._values[("rewards", "S", ("value",))] = [
            Decimal("0.40"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0.60"),
        ]
        handler = RewardsHandler(client=client)

        rewards, _reward_deltas, _reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_parent",
        )

        self.assertEqual(str(rewards["validator_reward"]["node1"]), "1")
        self.assertEqual(str(rewards["validator_reward"]["node2"]), "1")

    def test_build_tx_reward_outputs_splits_validator_rewards_with_commission_and_delegations(
        self,
    ):
        client = _FakeClient(
            validators=["node1"],
            validator_powers={"node1": Decimal("10")},
            reward_keys={"node1": "validator-reward"},
            commission_bps={"node1": 1000},
            self_bonds={"node1": Decimal("300")},
            delegations={
                ("alice", "node1"): Decimal("200"),
                ("bob", "node1"): Decimal("500"),
            },
            delegator_reward_keys={
                ("alice", "node1"): "alice-reward",
            },
        )
        client._values[("rewards", "S", ("value",))] = [
            Decimal("0.40"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0.60"),
        ]
        handler = RewardsHandler(client=client)

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_chi_to_split=100,
            contract="con_parent",
        )

        self.assertEqual(
            str(rewards["validator_reward"]["validator-reward"]), "0.74"
        )
        self.assertEqual(
            str(rewards["delegator_reward"]["alice-reward"]), "0.36"
        )
        self.assertEqual(str(rewards["delegator_reward"]["bob"]), "0.9")
        self.assertEqual(
            str(reward_deltas["currency.balances:validator-reward"]), "0.74"
        )
        self.assertEqual(
            str(reward_deltas["currency.balances:alice-reward"]), "0.36"
        )
        self.assertEqual(str(reward_deltas["currency.balances:bob"]), "0.9")
        self.assertEqual(
            [record["type"] for record in reward_records if "validator_key" in record],
            ["validator_reward", "delegator_reward", "delegator_reward"],
        )


if __name__ == "__main__":
    unittest.main()
