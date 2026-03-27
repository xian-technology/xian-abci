import base64
import json
import unittest
from unittest.mock import patch

from xian.validators import ValidatorHandler


def _validator_payload(entries):
    return {
        "result": {
            "validators": [
                {
                    "pub_key": {
                        "value": base64.b64encode(
                            bytes.fromhex(entry["validator"])
                        ).decode("ascii")
                    },
                    "voting_power": str(entry["power"]),
                }
                for entry in entries
            ]
        }
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeDriver:
    def __init__(self, values):
        self._values = values

    def get(self, key):
        return self._values.get(key)


class _FakeClient:
    def __init__(self, values):
        self.raw_driver = _FakeDriver(values)


class _FakeApp:
    def __init__(self, values):
        self.client = _FakeClient(values)


class ValidatorHandlerTests(unittest.TestCase):
    def test_build_validator_updates_adds_new_validator_with_configured_power(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                {
                    "masternodes.nodes": [validator],
                    f"masternodes.validator_power:{validator}": 25,
                }
            )
        )

        with patch(
            "xian.validators.urlopen",
            return_value=_FakeResponse(_validator_payload([])),
        ):
            updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 0)

        with patch(
            "xian.validators.urlopen",
            return_value=_FakeResponse(
                _validator_payload(
                    [{"validator": "22" * 32, "power": 10}]
                )
            ),
        ):
            updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0].power, 25)
        self.assertEqual(updates[1].power, 0)

    def test_build_validator_updates_changes_existing_power(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                {
                    "masternodes.nodes": [validator],
                    f"masternodes.validator_power:{validator}": 42,
                }
            )
        )

        with patch(
            "xian.validators.urlopen",
            return_value=_FakeResponse(
                _validator_payload([{"validator": validator, "power": 10}])
            ),
        ):
            updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].power, 42)

    def test_build_validator_updates_falls_back_to_default_power(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                {
                    "masternodes.nodes": [validator],
                }
            )
        )

        with patch(
            "xian.validators.urlopen",
            return_value=_FakeResponse(
                _validator_payload([{"validator": "22" * 32, "power": 10}])
            ),
        ):
            updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0].power, 10)
