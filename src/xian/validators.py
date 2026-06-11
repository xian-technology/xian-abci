import logging

from cometbft.abci.v1beta1.types_pb2 import ValidatorUpdate
from cometbft.crypto.v1.keys_pb2 import PublicKey


class ValidatorHandler:
    DEFAULT_VALIDATOR_POWER = 10

    def __init__(self, app):
        self.client = app.client

    def get_validators_from_state(self, *, committed: bool = False) -> dict[str, int]:
        read = self.client.raw_driver.value_from_disk if committed else self.client.raw_driver.get
        validators = read("validators.active_validators") or []
        desired = {}
        for validator in validators:
            power = read(f"validators.powers:{validator}")
            if power is None:
                power = self.DEFAULT_VALIDATOR_POWER
            if power <= 0:
                power = self.DEFAULT_VALIDATOR_POWER
            desired[validator] = int(power)
        return desired

    def to_bytes(self, data: str) -> bytes:
        return bytes.fromhex(data)

    def build_validator_updates(self, height) -> list[ValidatorUpdate]:
        del height
        validators_state = self.get_validators_from_state() or {}
        validators_committed = self.get_validators_from_state(committed=True) or {}
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
                logging.info(f"Updating {validator} in validator set to power {power}")
        for validator in validators_committed:
            if validator not in validators_state:
                updates.append(
                    ValidatorUpdate(
                        pub_key=PublicKey(ed25519=self.to_bytes(validator)),
                        power=0,
                    )
                )
                logging.info(f"Removing {validator} from validator set")

        return updates
