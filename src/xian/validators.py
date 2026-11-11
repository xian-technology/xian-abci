from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal

from cometbft.abci.v1beta1.types_pb2 import ValidatorUpdate
from cometbft.crypto.v1.keys_pb2 import PublicKey
from xian.formatting import vk_is_formatted

# Node-managed metadata: contract identifiers cannot start/end with underscores.
# Keeping it in the validators namespace includes it in app hashes and exports.
CONSENSUS_VALIDATORS_KEY = "validators.__consensus__"
# CometBFT rejects a validator set whose total power exceeds MaxInt64 / 8.
MAX_TOTAL_VOTING_POWER = (2**63 - 1) // 8


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
