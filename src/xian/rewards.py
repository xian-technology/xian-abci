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

    def _get_masternode_power(self, masternode: str):
        power = self.client.get_var(
            contract="masternodes",
            variable="validator_power",
            arguments=[masternode],
        )
        if power is None:
            return decimal.Decimal("1")
        normalized = self._as_decimal(power)
        if normalized <= 0:
            return decimal.Decimal("1")
        return normalized

    def _get_masternode_reward_key(self, masternode: str):
        reward_key = self.client.get_var(
            contract="masternodes",
            variable="reward_keys",
            arguments=[masternode],
        )
        if reward_key is None:
            return masternode
        if reward_key == "":
            return masternode
        return reward_key

    def _get_masternode_commission_bps(self, masternode: str):
        commission = self.client.get_var(
            contract="masternodes",
            variable="commission_bps",
            arguments=[masternode],
        )
        if commission is None:
            return decimal.Decimal("0")
        normalized = self._as_decimal(commission)
        if normalized < 0:
            return decimal.Decimal("0")
        if normalized > decimal.Decimal("10000"):
            return decimal.Decimal("10000")
        return normalized

    def _get_masternode_self_bond(self, masternode: str):
        value = self.client.get_var(
            contract="masternodes",
            variable="self_bond",
            arguments=[masternode],
        )
        if value is None:
            return decimal.Decimal("0")
        normalized = self._as_decimal(value)
        if normalized < 0:
            return decimal.Decimal("0")
        return normalized

    def _get_masternode_delegators(self, masternode: str):
        delegators = self.client.get_var(
            contract="masternodes",
            variable="delegator_lists",
            arguments=[masternode],
        )
        if not delegators:
            return []
        return delegators

    def _get_masternode_delegation(self, delegator: str, masternode: str):
        amount = self.client.get_var(
            contract="masternodes",
            variable="delegations",
            arguments=[delegator, masternode],
        )
        if amount is None:
            return decimal.Decimal("0")
        normalized = self._as_decimal(amount)
        if normalized < 0:
            return decimal.Decimal("0")
        return normalized

    def _get_delegator_reward_key(self, delegator: str, masternode: str):
        reward_key = self.client.get_var(
            contract="masternodes",
            variable="delegator_reward_keys",
            arguments=[delegator, masternode],
        )
        if reward_key is None or reward_key == "":
            return delegator
        return reward_key

    def build_validator_reward_outputs(self, masternode: str, total_reward):
        total_reward = self._as_decimal(total_reward)
        operator_reward_key = self._get_masternode_reward_key(masternode)
        commission_bps = self._get_masternode_commission_bps(masternode)
        self_bond = self._get_masternode_self_bond(masternode)

        delegator_weights: list[tuple[str, str, decimal.Decimal]] = []
        total_delegated = decimal.Decimal("0")
        for delegator in self._get_masternode_delegators(masternode):
            delegation = self._get_masternode_delegation(delegator, masternode)
            if delegation <= 0:
                continue
            total_delegated += delegation
            delegator_weights.append(
                (
                    delegator,
                    self._get_delegator_reward_key(delegator, masternode),
                    delegation,
                )
            )

        stake_base = self_bond + total_delegated
        if stake_base <= 0:
            amount = ContractingDecimal(str(total_reward))
            return (
                {operator_reward_key: amount},
                [
                    {
                        "type": "masternode_reward",
                        "recipient_key": operator_reward_key,
                        "source_contract": None,
                        "validator_key": masternode,
                        "value": amount,
                    }
                ],
            )

        commission = (total_reward * commission_bps) / decimal.Decimal("10000")
        remainder = total_reward - commission

        reward_mapping = defaultdict(lambda: ContractingDecimal("0"))
        reward_records = []

        operator_total = commission
        if self_bond > 0:
            operator_total += (remainder * self_bond) / stake_base

        if operator_total > 0:
            operator_amount = ContractingDecimal(str(operator_total))
            reward_mapping[operator_reward_key] += operator_amount
            reward_records.append(
                {
                    "type": "masternode_reward",
                    "recipient_key": operator_reward_key,
                    "source_contract": None,
                    "validator_key": masternode,
                    "value": operator_amount,
                }
            )

        allocated = operator_total
        for index, (delegator, reward_key, weight) in enumerate(
            delegator_weights
        ):
            if index == len(delegator_weights) - 1:
                share = total_reward - allocated
            else:
                share = (remainder * weight) / stake_base
                allocated += share

            share_amount = ContractingDecimal(str(share))
            reward_mapping[reward_key] += share_amount
            reward_records.append(
                {
                    "type": "delegator_reward",
                    "recipient_key": reward_key,
                    "source_contract": None,
                    "validator_key": masternode,
                    "delegator_key": delegator,
                    "value": share_amount,
                }
            )

        return dict(reward_mapping), reward_records

    def build_masternode_reward_outputs(
        self, total_stamps_to_split, participant_ratio, masternodes
    ):
        masternode_total = self._as_decimal(
            total_stamps_to_split
        ) * self._as_decimal(participant_ratio)

        weighted_nodes = []
        for masternode in masternodes:
            weighted_nodes.append(
                (
                    masternode,
                    self._get_masternode_power(masternode),
                )
            )

        if len(weighted_nodes) == 0:
            return {}, []

        total_weight = decimal.Decimal("0")
        for _, weight in weighted_nodes:
            total_weight += weight

        reward_mapping = defaultdict(lambda: ContractingDecimal("0"))
        reward_records = []
        allocated = decimal.Decimal("0")

        for index, (masternode, weight) in enumerate(weighted_nodes):
            if index == len(weighted_nodes) - 1:
                share = masternode_total - allocated
            else:
                share = (masternode_total * weight) / total_weight
                allocated += share

            share_mapping, share_records = self.build_validator_reward_outputs(
                masternode=masternode,
                total_reward=share,
            )
            for recipient_key, recipient_reward in share_mapping.items():
                reward_mapping[recipient_key] += recipient_reward
            reward_records.extend(share_records)

        return dict(reward_mapping), reward_records

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

        masternodes = (
            self.client.get_var(contract="masternodes", variable="nodes") or []
        )
        masternode_mapping, masternode_records = (
            self.build_masternode_reward_outputs(
                total_stamps_to_split=total_stamps_to_split,
                participant_ratio=master_ratio,
                masternodes=masternodes,
            )
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
            masternode_mapping,
            masternode_records,
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
        if stamp_rate in (None, 0) or foundation_owner is None:
            logger.error("Reward configuration is incomplete.")
            return None, {}, []

        (
            masternode_mapping,
            masternode_records,
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
            "delegator_reward": {},
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

        for record in masternode_records:
            masternode_amount = ContractingDecimal(
                str(record["value"] / stamp_rate)
            )
            recipient_key = record["recipient_key"]
            reward_bucket = (
                "delegator_reward"
                if record["type"] == "delegator_reward"
                else "masternode_reward"
            )
            existing_amount = rewards[reward_bucket].get(recipient_key)
            if existing_amount is None:
                rewards[reward_bucket][recipient_key] = masternode_amount
            else:
                rewards[reward_bucket][recipient_key] = (
                    existing_amount + masternode_amount
                )
            reward_deltas[f"currency.balances:{recipient_key}"] += (
                masternode_amount
            )
            normalized_record = {
                "type": record["type"],
                "recipient_key": recipient_key,
                "source_contract": None,
                "value": masternode_amount,
            }
            if "validator_key" in record:
                normalized_record["validator_key"] = record["validator_key"]
            if "delegator_key" in record:
                normalized_record["delegator_key"] = record["delegator_key"]
            reward_records.append(normalized_record)

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
            masternode_mapping,
            _masternode_records,
            foundation_reward,
            developer_mapping,
            _developer_records,
        ) = self.calculate_tx_output_rewards(
            total_stamps_to_split=stamp_rewards_amount,
            contract=stamp_rewards_contract,
        )

        stamp_cost = driver.get("stamp_cost.S:value")
        foundation_reward /= stamp_cost

        rewards = self._distribute_masternode_rewards(
            driver, masternode_mapping, stamp_cost
        )
        rewards.append(
            self._distribute_foundation_reward(driver, foundation_reward)
        )
        rewards.extend(
            self._distribute_developer_rewards(
                driver, developer_mapping, stamp_cost
            )
        )

        return rewards

    def _distribute_masternode_rewards(
        self, driver, masternode_mapping, stamp_cost
    ):
        rewards = []
        for recipient_key, reward in masternode_mapping.items():
            normalized_reward = reward / stamp_cost
            m_balance = driver.get(f"currency.balances:{recipient_key}") or 0
            m_balance_after = round(
                m_balance + normalized_reward, c.DUST_EXPONENT
            )
            rewards.append(
                driver.set(
                    f"currency.balances:{recipient_key}",
                    m_balance_after,
                )
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
