import decimal
from collections import defaultdict

from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal

from xian.constants import Constants as c


class RewardsHandler:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def _as_decimal(value):
        if isinstance(value, dict):
            value = value["__fixed__"]
        if isinstance(value, decimal.Decimal):
            return value
        return decimal.Decimal(str(value))

    def calculate_participant_reward(
        self, participant_ratio, number_of_participants, total_stamps_to_split
    ):
        number_of_participants = (
            number_of_participants if number_of_participants != 0 else 1
        )
        try:
            if isinstance(participant_ratio, dict):
                participant_ratio = participant_ratio["__fixed__"]
            reward = (
                decimal.Decimal(str(participant_ratio)) / number_of_participants
            ) * decimal.Decimal(str(total_stamps_to_split))
            rounded_reward = round(reward, c.DUST_EXPONENT)
        except Exception as e:
            logger.error(f"Error in calculating reward: {e}")
            rounded_reward = 0
        return ContractingDecimal(str(rounded_reward))

    def find_developer_and_reward(
        self,
        total_stamps_to_split,
        contract,
        developer_ratio,
        foundation_owner,
        contract_costs=None,
    ):
        developer_ratio = self._as_decimal(developer_ratio)
        developer_total = (
            self._as_decimal(total_stamps_to_split) * developer_ratio
        )

        send_map = defaultdict(lambda: ContractingDecimal("0"))
        developer_records = []
        weights = []
        if isinstance(contract_costs, dict):
            for source_contract, weight in sorted(contract_costs.items()):
                normalized_weight = self._as_decimal(weight)
                if normalized_weight > 0:
                    weights.append((source_contract, normalized_weight))

        if not weights:
            weights = [(contract, decimal.Decimal("1"))]

        total_weight = sum(weight for _, weight in weights)
        allocated = decimal.Decimal("0")

        for index, (source_contract, weight) in enumerate(weights):
            if index == len(weights) - 1:
                share = developer_total - allocated
            else:
                share = (developer_total * weight) / total_weight
                allocated += share

            recipient = self.client.get_var(
                contract=source_contract, variable="__developer__"
            )
            if not recipient or recipient == "sys":
                recipient = foundation_owner

            share_amount = ContractingDecimal(str(share))
            send_map[recipient] += share_amount
            developer_records.append(
                {
                    "type": "developer_reward",
                    "recipient_key": recipient,
                    "source_contract": source_contract,
                    "value": share_amount,
                }
            )

        return dict(send_map), developer_records

    def calculate_tx_output_rewards(
        self,
        total_stamps_to_split,
        contract,
        *,
        foundation_owner=None,
        contract_costs=None,
    ):
        if not self.client.get_var(
            contract="rewards", variable="S", arguments=["value"]
        ):
            logger.error("Rewards not set up.")
            return 0, 0, {}, []
        try:
            master_ratio, burn_ratio, foundation_ratio, developer_ratio = (
                self.client.get_var(
                    contract="rewards", variable="S", arguments=["value"]
                )
            )
        except TypeError:
            raise NotImplementedError(
                "Driver could not get value for key rewards.S:value. Try setting up rewards."
            )

        if foundation_owner is None:
            foundation_owner = self.client.get_var(
                contract="foundation", variable="owner"
            )

        master_reward = self.calculate_participant_reward(
            participant_ratio=master_ratio,
            number_of_participants=len(
                self.client.get_var(contract="masternodes", variable="nodes")
            ),
            total_stamps_to_split=total_stamps_to_split,
        )

        foundation_reward = self.calculate_participant_reward(
            participant_ratio=foundation_ratio,
            number_of_participants=1,
            total_stamps_to_split=total_stamps_to_split,
        )

        developer_mapping, developer_records = self.find_developer_and_reward(
            total_stamps_to_split=total_stamps_to_split,
            contract=contract,
            developer_ratio=developer_ratio,
            foundation_owner=foundation_owner,
            contract_costs=contract_costs,
        )

        return (
            master_reward,
            foundation_reward,
            developer_mapping,
            developer_records,
        )

    def build_tx_reward_outputs(
        self,
        total_stamps_to_split,
        contract,
        *,
        contract_costs=None,
    ):
        reward_split = self.client.get_var(
            contract="rewards", variable="S", arguments=["value"]
        )
        if not reward_split or total_stamps_to_split <= 0:
            return None, {}, []

        stamp_rate = self.client.get_var(
            contract="stamp_cost", variable="S", arguments=["value"]
        )
        foundation_owner = self.client.get_var(
            contract="foundation", variable="owner"
        )
        masternodes = (
            self.client.get_var(contract="masternodes", variable="nodes") or []
        )

        if stamp_rate in (None, 0) or foundation_owner is None:
            logger.error("Reward configuration is incomplete.")
            return None, {}, []

        (
            master_reward,
            foundation_reward,
            developer_mapping,
            developer_records,
        ) = self.calculate_tx_output_rewards(
            total_stamps_to_split=total_stamps_to_split,
            contract=contract,
            foundation_owner=foundation_owner,
            contract_costs=contract_costs,
        )

        rewards = {
            "masternode_reward": {},
            "foundation_reward": {},
            "developer_reward": {},
        }
        reward_deltas = defaultdict(lambda: ContractingDecimal("0"))
        reward_records = []

        if foundation_reward:
            foundation_amount = ContractingDecimal(
                str(foundation_reward / stamp_rate)
            )
            rewards["foundation_reward"][foundation_owner] = foundation_amount
            reward_deltas[f"currency.balances:{foundation_owner}"] += (
                foundation_amount
            )
            reward_records.append(
                {
                    "type": "foundation_reward",
                    "recipient_key": foundation_owner,
                    "source_contract": None,
                    "value": foundation_amount,
                }
            )

        if master_reward:
            masternode_amount = ContractingDecimal(
                str(master_reward / stamp_rate)
            )
            for masternode in masternodes:
                rewards["masternode_reward"][masternode] = masternode_amount
                reward_deltas[f"currency.balances:{masternode}"] += (
                    masternode_amount
                )
                reward_records.append(
                    {
                        "type": "masternode_reward",
                        "recipient_key": masternode,
                        "source_contract": None,
                        "value": masternode_amount,
                    }
                )

        for developer, reward in developer_mapping.items():
            developer_amount = ContractingDecimal(str(reward / stamp_rate))
            existing_amount = rewards["developer_reward"].get(developer)
            if existing_amount is None:
                rewards["developer_reward"][developer] = developer_amount
            else:
                rewards["developer_reward"][developer] = (
                    existing_amount + developer_amount
                )
            reward_deltas[f"currency.balances:{developer}"] += developer_amount

        for record in developer_records:
            reward_records.append(
                {
                    "type": record["type"],
                    "recipient_key": record["recipient_key"],
                    "source_contract": record["source_contract"],
                    "value": ContractingDecimal(
                        str(record["value"] / stamp_rate)
                    ),
                }
            )

        if not any(rewards.values()):
            return None, {}, reward_records

        return rewards, dict(reward_deltas), reward_records

    def distribute_rewards(self, stamp_rewards_amount, stamp_rewards_contract):
        if (
            not self.client.get_var(
                contract="rewards", variable="S", arguments=["value"]
            )
            or stamp_rewards_amount <= 0
        ):
            return []

        driver = self.client.raw_driver
        (
            master_reward,
            foundation_reward,
            developer_mapping,
            _developer_records,
        ) = self.calculate_tx_output_rewards(
            total_stamps_to_split=stamp_rewards_amount,
            contract=stamp_rewards_contract,
        )

        stamp_cost = driver.get("stamp_cost.S:value")
        master_reward /= stamp_cost
        foundation_reward /= stamp_cost

        rewards = self._distribute_masternode_rewards(driver, master_reward)
        rewards.append(
            self._distribute_foundation_reward(driver, foundation_reward)
        )
        rewards.extend(
            self._distribute_developer_rewards(
                driver, developer_mapping, stamp_cost
            )
        )

        return rewards

    def _distribute_masternode_rewards(self, driver, master_reward):
        rewards = []
        for m in driver.get("masternodes.nodes"):
            m_balance = driver.get(f"currency.balances:{m}") or 0
            m_balance_after = round(m_balance + master_reward, c.DUST_EXPONENT)
            rewards.append(
                driver.set(f"currency.balances:{m}", m_balance_after)
            )
        return rewards

    def _distribute_foundation_reward(self, driver, foundation_reward):
        foundation_wallet = driver.get("foundation.owner")
        foundation_balance = (
            driver.get(f"currency.balances:{foundation_wallet}") or 0
        )
        foundation_balance_after = round(
            foundation_balance + foundation_reward, c.DUST_EXPONENT
        )
        return driver.set(
            f"currency.balances:{foundation_wallet}", foundation_balance_after
        )

    def _distribute_developer_rewards(
        self, driver, developer_mapping, stamp_cost
    ):
        rewards = []
        for recipient, amount in developer_mapping.items():
            if recipient == "sys" or recipient is None:
                recipient = driver.get("foundation.owner")
            dev_reward = round(amount / stamp_cost, c.DUST_EXPONENT)
            recipient_balance = (
                driver.get(f"currency.balances:{recipient}") or 0
            )
            recipient_balance_after = round(
                recipient_balance + dev_reward, c.DUST_EXPONENT
            )
            rewards.append(
                driver.set(
                    f"currency.balances:{recipient}", recipient_balance_after
                )
            )
        return rewards

    def distribute_static_rewards(self, master_reward, foundation_reward):
        rewards = []
        driver = self.client.raw_driver

        rewards.extend(
            self._distribute_masternode_rewards(driver, master_reward)
        )
        rewards.append(
            self._distribute_foundation_reward(driver, foundation_reward)
        )

        return rewards
