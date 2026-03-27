import base64
import json
import logging
from urllib.request import urlopen

from cometbft.abci.v1beta1.types_pb2 import ValidatorUpdate
from cometbft.crypto.v1.keys_pb2 import PublicKey


class ValidatorHandler:
    def __init__(self, app):
        self.client = app.client

    def get_validators_from_state(self) -> dict[str, int]:
        validators = self.client.raw_driver.get("masternodes.nodes") or []
        desired = {}
        for validator in validators:
            power = self.client.raw_driver.get(
                f"masternodes.validator_power:{validator}"
            )
            if power is None:
                power = 10
            if power <= 0:
                power = 10
            desired[validator] = int(power)
        return desired

    def get_tendermint_validators(self) -> dict[str, int]:
        try:
            with urlopen("http://localhost:26657/validators") as response:
                payload = json.loads(response.read().decode("utf-8"))
            validators = {}
            for validator in payload["result"]["validators"]:
                voting_power = int(validator["voting_power"])
                if voting_power <= 0:
                    continue
                validators[
                    base64.b64decode(validator["pub_key"]["value"]).hex()
                ] = voting_power
        except Exception:
            validators = {}
        return validators

    def to_bytes(self, data: str) -> bytes:
        return bytes.fromhex(data)

    def build_validator_updates(self, height) -> list[ValidatorUpdate]:
        validators_state = self.get_validators_from_state() or {}
        validators_tendermint = self.get_tendermint_validators()
        if len(validators_tendermint) == 0:
            logging.error("Failed to get validators from tendermint")
            return []
        updates = []
        for validator, power in validators_state.items():
            current_power = validators_tendermint.get(validator)
            if current_power != power:
                updates.append(
                    ValidatorUpdate(
                        pub_key=PublicKey(ed25519=self.to_bytes(validator)),
                        power=power,
                    )
                )
                logging.info(
                    f"Updating {validator} in tendermint validators to power {power}"
                )
        for validator in validators_tendermint:
            if validator not in validators_state:
                updates.append(
                    ValidatorUpdate(
                        pub_key=PublicKey(ed25519=self.to_bytes(validator)),
                        power=0,
                    )
                )
                logging.info(f"Removing {validator} from tendermint validators")

        return updates
