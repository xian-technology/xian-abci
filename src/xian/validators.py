from loguru import logger

from cometbft.abci.v1beta1.types_pb2 import ValidatorUpdate
from cometbft.crypto.v1.keys_pb2 import PublicKey
from xian.formatting import vk_is_formatted

# Node-managed metadata: contract identifiers cannot start/end with underscores.
# Keeping it in the validators namespace includes it in app hashes and exports.
CONSENSUS_VALIDATORS_KEY = "validators.__consensus__"


class ValidatorHandler:
    DEFAULT_VALIDATOR_POWER = 10

    def __init__(self, app):
        self.client = app.client

    def get_validators_from_state(self, *, committed: bool = False) -> dict[str, int] | None:
        read = self.client.raw_driver.value_from_disk if committed else self.client.raw_driver.get
        validators = read("validators.active_validators") or []
        if not isinstance(validators, list):
            logger.bind(stage="validator_selection", reason_code="invalid_validator_list").warning(
                "Ignoring invalid active validator list"
            )
            return None
        desired = {}
        for validator in validators:
            if not vk_is_formatted(validator):
                logger.bind(
                    stage="validator_selection",
                    reason_code="invalid_validator_key",
                    value_type=type(validator).__name__,
                ).warning("Ignoring invalid validator key in state")
                continue
            power = self.validator_power(read(f"validators.powers:{validator}"))
            key = validator.lower()
            if key in desired and desired[key] != power:
                # Aliases identify one consensus key, never two validators.
                # Conflicting power must not depend on list ordering.
                logger.bind(
                    stage="validator_selection",
                    reason_code="conflicting_validator_powers",
                    validator=key,
                ).warning("Ignoring validator selection with conflicting powers")
                return None
            desired[key] = power
        return desired

    def validator_power(self, power) -> int:
        if power is None or power <= 0:
            return self.DEFAULT_VALIDATOR_POWER
        return int(power)

    def committed_validators(self) -> dict[str, int]:
        validators = self.client.raw_driver.value_from_disk(CONSENSUS_VALIDATORS_KEY)
        if validators is None:
            # Bootstrap existing chains from their committed contract state.
            return self.get_validators_from_state(committed=True) or {}
        return validators

    def to_bytes(self, data: str) -> bytes:
        return bytes.fromhex(data)

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
        if validators_state:
            # Stage this with the block's writes, so rejected selections still
            # retain the correct baseline after Commit, restart or state sync.
            self.client.raw_driver.set(CONSENSUS_VALIDATORS_KEY, validators_state)
        updates = []
        for validator, power in validators_state.items():
            current_power = validators_committed.get(validator)
            if current_power != power:
                updates.append(
                    ValidatorUpdate(
                        pub_key=PublicKey(ed25519=self.to_bytes(validator)),
                        power=power,
                    )
                )
                logger.bind(
                    stage="validator_update", validator=validator, power=power, block_height=height
                ).info("Updating validator voting power")
        for validator in validators_committed:
            if validator not in validators_state:
                updates.append(
                    ValidatorUpdate(
                        pub_key=PublicKey(ed25519=self.to_bytes(validator)),
                        power=0,
                    )
                )
                logger.bind(
                    stage="validator_update", validator=validator, block_height=height
                ).info("Removing validator from effective set")

        return updates
