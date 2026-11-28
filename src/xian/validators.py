from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal

from tendermint.abci.types_pb2 import ValidatorUpdate
from xian.formatting import vk_is_formatted
from xian.validator_keys import (
    MAX_MEMBERS,
    MAX_TOTAL_VOTING_POWER,
    ValidatorSuite,
    state_suite,
    validate_genesis_validator_keys,
)

# Node-managed metadata: contract identifiers cannot start/end with underscores.
# Keeping it in the validators namespace includes it in app hashes and exports.
CONSENSUS_VALIDATORS_KEY = "validators.__consensus__"


class ValidatorHandler:
    DEFAULT_VALIDATOR_POWER = 10

    def __init__(self, app):
        self.client = app.client
        genesis = getattr(app, "genesis", {})
        self.suite = ValidatorSuite.from_genesis(genesis)
        if "abci_genesis" in genesis:
            validate_genesis_validator_keys(genesis)
            read = self.client.raw_driver.value_from_disk
            if (
                read("validators.active_validators") is not None
                and state_suite(read) is not self.suite
            ):
                raise ValueError("persisted validator key type disagrees with genesis")

    def get_validators_from_state(self, *, committed: bool = False) -> dict[str, int] | None:
        read = self.client.raw_driver.value_from_disk if committed else self.client.raw_driver.get
        validators = read("validators.active_validators") or []
        if not isinstance(validators, list):
            logger.bind(stage="validator_selection", reason_code="invalid_validator_list").warning(
                "Ignoring invalid active validator list"
            )
            return None
        if self.suite is ValidatorSuite.ML_DSA_65 and len(validators) > MAX_MEMBERS:
            return self._reject_selection("too_many_ml_dsa_validators")
        desired = {}
        for validator in validators:
            if not vk_is_formatted(validator):
                if self.suite is ValidatorSuite.ML_DSA_65:
                    return self._reject_selection("invalid_validator_member")
                logger.bind(
                    stage="validator_selection",
                    reason_code="invalid_validator_key",
                    value_type=type(validator).__name__,
                ).warning("Ignoring invalid validator key in state")
                continue
            # Keys are canonical lowercase hex, so one key is one validator.
            power = self.validator_power(read(f"validators.powers:{validator}"))
            if power is None:
                return self._reject_selection("invalid_validator_power")
            desired[validator] = power
        if sum(desired.values()) > MAX_TOTAL_VOTING_POWER:
            return self._reject_selection("total_voting_power_exceeds_cometbft_limit")
        return desired

    @staticmethod
    def _reject_selection(reason_code: str) -> None:
        # CometBFT halts on a validator set it cannot apply; keep the
        # effective set instead, as for every other invalid selection.
        logger.bind(stage="validator_selection", reason_code=reason_code).warning(
            "Ignoring validator selection that CometBFT cannot apply"
        )
        return None

    def validator_power(self, power) -> int | None:
        if power is None:
            return self.DEFAULT_VALIDATOR_POWER
        if isinstance(power, bool) or not isinstance(power, int | ContractingDecimal):
            return None
        if power <= 0:
            return self.DEFAULT_VALIDATOR_POWER
        normalized = int(power)
        # A fractional power truncating to zero would silently remove the
        # validator; oversized powers are rejected by the total check.
        if normalized <= 0:
            return None
        return normalized

    def committed_validators(self) -> dict[str, int]:
        validators = self.client.raw_driver.value_from_disk(CONSENSUS_VALIDATORS_KEY)
        if validators is None:
            # Bootstrap existing chains from their committed contract state.
            return self.get_validators_from_state(committed=True) or {}
        return validators

    def update(self, member: str, power: int, *, committed: bool = False) -> ValidatorUpdate:
        driver = self.client.raw_driver
        read = driver.value_from_disk if committed else driver.get
        raw = self.suite.member_key(member, read)
        return ValidatorUpdate(pub_key=self.suite.proto_key(raw), power=power)

    def genesis_validators(self) -> list[ValidatorUpdate]:
        """Return the set the application will use as the baseline for updates."""
        validators = self.get_validators_from_state(committed=True) or {}
        if not validators and self.suite is ValidatorSuite.ML_DSA_65:
            raise ValueError("empty ML-DSA genesis validator set")
        return [self.update(member, power, committed=True) for member, power in validators.items()]

    def build_validator_updates(self, height) -> list[ValidatorUpdate]:
        validators_state = self.get_validators_from_state() or {}
        validators_committed = self.committed_validators()
        if not validators_state:
            logger.bind(
                stage="validator_selection",
                reason_code="empty_validator_selection",
                block_height=height,
            ).warning("Retaining effective validator set because no valid selection remains")
            validators_state = validators_committed
        try:
            updates = [
                self.update(member, power)
                for member, power in validators_state.items()
                if validators_committed.get(member) != power
            ]
        except ValueError:
            self._reject_selection("invalid_consensus_key_binding")
            validators_state = validators_committed
            updates = []
        updates.extend(
            self.update(member, 0, committed=True)
            for member in validators_committed
            if member not in validators_state
        )
        if validators_state:
            self.client.raw_driver.set(CONSENSUS_VALIDATORS_KEY, validators_state)
        if updates:
            logger.bind(stage="validator_update", block_height=height, count=len(updates)).info(
                "Updating consensus validators"
            )
        return updates
