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
        self, participant_ratio, number_of_participants, total_chi_to_split
    ):
        number_of_participants = number_of_participants if number_of_participants != 0 else 1
        try:
            if isinstance(participant_ratio, dict):
                participant_ratio = participant_ratio["__fixed__"]
            reward = (
                decimal.Decimal(str(participant_ratio)) / number_of_participants
            ) * decimal.Decimal(str(total_chi_to_split))
            rounded_reward = round(reward, c.DUST_EXPONENT)
        except Exception as e:
            logger.error(f"Error in calculating reward: {e}")
            rounded_reward = 0
        return ContractingDecimal(str(rounded_reward))

    def _get_validator_power(self, validator: str):
        power = self.client.get_var(
            contract="validators",
            variable="powers",
            arguments=[validator],
        )
        if power is None:
            return decimal.Decimal("1")
        normalized = self._as_decimal(power)
        if normalized <= 0:
            return decimal.Decimal("1")
        return normalized

    def _get_validator_reward_key(self, validator: str):
        reward_key = self.client.get_var(
            contract="validators",
            variable="reward_keys",
            arguments=[validator],
        )
        if reward_key is None:
            return validator
        if reward_key == "":
            return validator
        return reward_key

    def _get_validator_commission_bps(self, validator: str):
        commission = self.client.get_var(
            contract="validators",
            variable="commission_bps",
            arguments=[validator],
        )
        if commission is None:
            return decimal.Decimal("0")
        normalized = self._as_decimal(commission)
        if normalized < 0:
            return decimal.Decimal("0")
        if normalized > decimal.Decimal("10000"):
            return decimal.Decimal("10000")
        return normalized

    def _get_validator_self_bond(self, validator: str):
        value = self.client.get_var(
            contract="validators",
            variable="self_bond",
            arguments=[validator],
        )
        if value is None:
            return decimal.Decimal("0")
        normalized = self._as_decimal(value)
        if normalized < 0:
            return decimal.Decimal("0")
        return normalized

    def _get_validator_delegators(self, validator: str):
        delegators = self.client.get_var(
            contract="validators",
            variable="delegator_lists",
            arguments=[validator],
        )
        if not delegators:
            return []
        return delegators

    def _get_validator_delegation(self, delegator: str, validator: str):
        amount = self.client.get_var(
            contract="validators",
            variable="delegations",
            arguments=[delegator, validator],
        )
        if amount is None:
            return decimal.Decimal("0")
        normalized = self._as_decimal(amount)
        if normalized < 0:
            return decimal.Decimal("0")
        return normalized

    def _get_delegator_reward_key(self, delegator: str, validator: str):
        reward_key = self.client.get_var(
            contract="validators",
            variable="delegator_reward_keys",
            arguments=[delegator, validator],
        )
        if reward_key is None or reward_key == "":
            return delegator
        return reward_key

    def build_single_validator_reward_outputs(self, validator: str, total_reward):
        total_reward = self._as_decimal(total_reward)
        operator_reward_key = self._get_validator_reward_key(validator)
        commission_bps = self._get_validator_commission_bps(validator)
        self_bond = self._get_validator_self_bond(validator)

        delegator_weights: list[tuple[str, str, decimal.Decimal]] = []
        total_delegated = decimal.Decimal("0")
        for delegator in self._get_validator_delegators(validator):
            delegation = self._get_validator_delegation(delegator, validator)
            if delegation <= 0:
                continue
            total_delegated += delegation
            delegator_weights.append(
                (
                    delegator,
                    self._get_delegator_reward_key(delegator, validator),
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
                        "type": "validator_reward",
                        "recipient_key": operator_reward_key,
                        "source_contract": None,
                        "validator_key": validator,
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
                    "type": "validator_reward",
                    "recipient_key": operator_reward_key,
                    "source_contract": None,
                    "validator_key": validator,
                    "value": operator_amount,
                }
            )

        allocated = operator_total
        for index, (delegator, reward_key, weight) in enumerate(delegator_weights):
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
                    "validator_key": validator,
                    "delegator_key": delegator,
                    "value": share_amount,
                }
            )

        return dict(reward_mapping), reward_records

    def build_validator_set_reward_outputs(
        self,
        total_chi_to_split,
        participant_ratio,
        validators,
        additional_chi_to_split=0,
    ):
        validator_total = (
            self._as_decimal(total_chi_to_split) * self._as_decimal(participant_ratio)
        ) + self._as_decimal(additional_chi_to_split)

        weighted_nodes = []
        for validator in validators:
            weighted_nodes.append(
                (
                    validator,
                    self._get_validator_power(validator),
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

        for index, (validator, weight) in enumerate(weighted_nodes):
            if index == len(weighted_nodes) - 1:
                share = validator_total - allocated
            else:
                share = (validator_total * weight) / total_weight
                allocated += share

            share_mapping, share_records = self.build_single_validator_reward_outputs(
                validator=validator,
                total_reward=share,
            )
            for recipient_key, recipient_reward in share_mapping.items():
                reward_mapping[recipient_key] += recipient_reward
            reward_records.extend(share_records)

        return dict(reward_mapping), reward_records

    def find_developer_and_reward(
        self,
        total_chi_to_split,
        contract,
        developer_ratio,
        contract_costs=None,
    ):
        developer_ratio = self._as_decimal(developer_ratio)
        developer_total = self._as_decimal(total_chi_to_split) * developer_ratio

        send_map = defaultdict(lambda: ContractingDecimal("0"))
        developer_records = []
        redirected_validator_reward = decimal.Decimal("0")
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

            recipient = self.client.get_var(contract=source_contract, variable="__developer__")
            if not recipient or recipient == "sys":
                redirected_validator_reward += share
                continue

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

        return dict(send_map), developer_records, redirected_validator_reward

    def calculate_tx_output_rewards(
        self,
        total_chi_to_split,
        contract,
        *,
        contract_costs=None,
    ):
        if not self.client.get_var(contract="rewards", variable="S", arguments=["value"]):
            logger.error("Rewards not set up.")
            return 0, 0, {}, []
        try:
            validator_ratio, _burn_ratio, foundation_ratio, developer_ratio = self.client.get_var(
                contract="rewards", variable="S", arguments=["value"]
            )
        except TypeError:
            raise NotImplementedError(
                "Driver could not get value for key rewards.S:value. Try setting up rewards."
            )

        foundation_reward = self.calculate_participant_reward(
            participant_ratio=foundation_ratio,
            number_of_participants=1,
            total_chi_to_split=total_chi_to_split,
        )

        (
            developer_mapping,
            developer_records,
            redirected_validator_reward,
        ) = self.find_developer_and_reward(
            total_chi_to_split=total_chi_to_split,
            contract=contract,
            developer_ratio=developer_ratio,
            contract_costs=contract_costs,
        )

        validators = self.client.get_var(contract="validators", variable="active_validators") or []
        validator_mapping, validator_records = self.build_validator_set_reward_outputs(
            total_chi_to_split=total_chi_to_split,
            participant_ratio=validator_ratio,
            validators=validators,
            additional_chi_to_split=redirected_validator_reward,
        )

        return (
            validator_mapping,
            validator_records,
            foundation_reward,
            developer_mapping,
            developer_records,
        )

    def build_tx_reward_outputs(
        self,
        total_chi_to_split,
        contract,
        *,
        contract_costs=None,
    ):
        reward_split = self.client.get_var(contract="rewards", variable="S", arguments=["value"])
        if not reward_split or total_chi_to_split <= 0:
            return None, {}, []

        chi_rate = self.client.get_var(contract="chi_cost", variable="S", arguments=["value"])
        foundation_owner = self.client.get_var(contract="foundation", variable="owner")
        if chi_rate in (None, 0) or foundation_owner is None:
            logger.error("Reward configuration is incomplete.")
            return None, {}, []

        (
            validator_mapping,
            validator_records,
            foundation_reward,
            developer_mapping,
            developer_records,
        ) = self.calculate_tx_output_rewards(
            total_chi_to_split=total_chi_to_split,
            contract=contract,
            contract_costs=contract_costs,
        )

        rewards = {
            "validator_reward": {},
            "delegator_reward": {},
            "foundation_reward": {},
            "developer_reward": {},
        }
        reward_deltas = defaultdict(lambda: ContractingDecimal("0"))
        reward_records = []

        if foundation_reward:
            foundation_amount = ContractingDecimal(str(foundation_reward / chi_rate))
            rewards["foundation_reward"][foundation_owner] = foundation_amount
            reward_deltas[f"currency.balances:{foundation_owner}"] += foundation_amount
            reward_records.append(
                {
                    "type": "foundation_reward",
                    "recipient_key": foundation_owner,
                    "source_contract": None,
                    "value": foundation_amount,
                }
            )

        for record in validator_records:
            validator_amount = ContractingDecimal(str(record["value"] / chi_rate))
            recipient_key = record["recipient_key"]
            reward_bucket = (
                "delegator_reward" if record["type"] == "delegator_reward" else "validator_reward"
            )
            existing_amount = rewards[reward_bucket].get(recipient_key)
            if existing_amount is None:
                rewards[reward_bucket][recipient_key] = validator_amount
            else:
                rewards[reward_bucket][recipient_key] = existing_amount + validator_amount
            reward_deltas[f"currency.balances:{recipient_key}"] += validator_amount
            normalized_record = {
                "type": record["type"],
                "recipient_key": recipient_key,
                "source_contract": None,
                "value": validator_amount,
            }
            if "validator_key" in record:
                normalized_record["validator_key"] = record["validator_key"]
            if "delegator_key" in record:
                normalized_record["delegator_key"] = record["delegator_key"]
            reward_records.append(normalized_record)

        for developer, reward in developer_mapping.items():
            developer_amount = ContractingDecimal(str(reward / chi_rate))
            existing_amount = rewards["developer_reward"].get(developer)
            if existing_amount is None:
                rewards["developer_reward"][developer] = developer_amount
            else:
                rewards["developer_reward"][developer] = existing_amount + developer_amount
            reward_deltas[f"currency.balances:{developer}"] += developer_amount

        for record in developer_records:
            reward_records.append(
                {
                    "type": record["type"],
                    "recipient_key": record["recipient_key"],
                    "source_contract": record["source_contract"],
                    "value": ContractingDecimal(str(record["value"] / chi_rate)),
                }
            )

        if not any(rewards.values()):
            return None, {}, reward_records

        return rewards, dict(reward_deltas), reward_records

    def distribute_rewards(self, chi_rewards_amount, chi_rewards_contract):
        if (
            not self.client.get_var(contract="rewards", variable="S", arguments=["value"])
            or chi_rewards_amount <= 0
        ):
            return []

        driver = self.client.raw_driver
        (
            validator_mapping,
            _validator_records,
            foundation_reward,
            developer_mapping,
            _developer_records,
        ) = self.calculate_tx_output_rewards(
            total_chi_to_split=chi_rewards_amount,
            contract=chi_rewards_contract,
        )

        chi_cost = driver.get("chi_cost.S:value")
        foundation_reward /= chi_cost

        rewards = self._distribute_validator_rewards(driver, validator_mapping, chi_cost)
        rewards.append(self._distribute_foundation_reward(driver, foundation_reward))
        rewards.extend(self._distribute_developer_rewards(driver, developer_mapping, chi_cost))

        return rewards

    def _distribute_validator_rewards(self, driver, validator_mapping, chi_cost):
        rewards = []
        for recipient_key, reward in validator_mapping.items():
            normalized_reward = reward / chi_cost
            m_balance = driver.get(f"currency.balances:{recipient_key}") or 0
            m_balance_after = round(m_balance + normalized_reward, c.DUST_EXPONENT)
            rewards.append(
                driver.set(
                    f"currency.balances:{recipient_key}",
                    m_balance_after,
                )
            )
        return rewards

    def _distribute_foundation_reward(self, driver, foundation_reward):
        foundation_wallet = driver.get("foundation.owner")
        foundation_balance = driver.get(f"currency.balances:{foundation_wallet}") or 0
        foundation_balance_after = round(foundation_balance + foundation_reward, c.DUST_EXPONENT)
        return driver.set(f"currency.balances:{foundation_wallet}", foundation_balance_after)

    def _distribute_developer_rewards(self, driver, developer_mapping, chi_cost):
        rewards = []
        for recipient, amount in developer_mapping.items():
            dev_reward = round(amount / chi_cost, c.DUST_EXPONENT)
            recipient_balance = driver.get(f"currency.balances:{recipient}") or 0
            recipient_balance_after = round(recipient_balance + dev_reward, c.DUST_EXPONENT)
            rewards.append(driver.set(f"currency.balances:{recipient}", recipient_balance_after))
        return rewards
