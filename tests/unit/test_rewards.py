import unittest
from decimal import Decimal

from xian.rewards import RewardsHandler


class _FakeClient:
    def __init__(self, developers=None):
        developers = developers or {
            "con_parent": "alice",
            "con_child": "bob",
        }
        self._values = {
            ("rewards", "S", ("value",)): [
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("1"),
            ],
            ("stamp_cost", "S", ("value",)): Decimal("20"),
            ("foundation", "owner", ()): "foundation",
            ("masternodes", "nodes", ()): [],
        }
        for contract, developer in developers.items():
            self._values[(contract, "__developer__", ())] = developer

    def get_var(self, contract, variable, arguments=None):
        args = tuple(arguments or ())
        return self._values.get((contract, variable, args))


class RewardsHandlerTests(unittest.TestCase):
    def test_build_tx_reward_outputs_splits_developer_rewards_by_contract_cost(
        self,
    ):
        handler = RewardsHandler(client=_FakeClient())

        rewards, reward_deltas, reward_records = handler.build_tx_reward_outputs(
            total_stamps_to_split=100,
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
            total_stamps_to_split=100,
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


if __name__ == "__main__":
    unittest.main()
