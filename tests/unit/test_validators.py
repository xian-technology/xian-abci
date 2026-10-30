import unittest

import pytest

from xian.validators import ValidatorHandler


class _FakeDriver:
    def __init__(self, current_values, committed_values=None):
        self._current_values = dict(current_values)
        self._committed_values = (
            dict(committed_values) if committed_values is not None else dict(current_values)
        )

    def get(self, key):
        return self._current_values.get(key)

    def value_from_disk(self, key):
        return self._committed_values.get(key)

    def set(self, key, value):
        self._current_values[key] = value

    def commit(self):
        self._committed_values = dict(self._current_values)


class _FakeClient:
    def __init__(self, current_values, committed_values=None):
        self.raw_driver = _FakeDriver(current_values, committed_values)


class _FakeApp:
    def __init__(self, current_values, committed_values=None):
        self.client = _FakeClient(current_values, committed_values)


class ValidatorHandlerTests(unittest.TestCase):
    def test_build_validator_updates_adds_new_validator_with_configured_power(self):
        validator = "11" * 32
        stale_validator = "22" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={
                    "validators.active_validators": [validator],
                    f"validators.powers:{validator}": 25,
                },
                committed_values={
                    "validators.active_validators": [stale_validator],
                    f"validators.powers:{stale_validator}": 10,
                },
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0].power, 25)
        self.assertEqual(updates[1].power, 0)

    def test_build_validator_updates_changes_existing_power(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={
                    "validators.active_validators": [validator],
                    f"validators.powers:{validator}": 42,
                },
                committed_values={
                    "validators.active_validators": [validator],
                    f"validators.powers:{validator}": 10,
                },
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].power, 42)

    def test_build_validator_updates_falls_back_to_default_power(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={
                    "validators.active_validators": [validator],
                },
                committed_values={"validators.active_validators": []},
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].power, 10)

    def test_build_validator_updates_removes_validator_missing_from_current_state(self):
        validator = "11" * 32
        survivor = "22" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={"validators.active_validators": [survivor]},
                committed_values={
                    "validators.active_validators": [validator, survivor],
                    f"validators.powers:{validator}": 10,
                },
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].power, 0)


CONSENSUS_KEY = "validators.__consensus__"
VALIDATOR = "ab" * 32
REPLACEMENT = "cd" * 32


def update_values(updates):
    return [(update.pub_key.ed25519.hex(), update.power) for update in updates]


@pytest.mark.parametrize("invalid", ["con_registrar", "11" * 31, "g" * 64, " " * 64, 7, None, []])
def test_malformed_identities_do_not_abort_valid_validator_updates(invalid):
    app = _FakeApp(
        {"validators.active_validators": [VALIDATOR, invalid, REPLACEMENT]},
        {"validators.active_validators": [VALIDATOR]},
    )
    assert update_values(ValidatorHandler(app).build_validator_updates(1)) == [(REPLACEMENT, 10)]


def test_uppercase_identity_uses_original_power_and_canonical_consensus_key():
    app = _FakeApp(
        {
            "validators.active_validators": [REPLACEMENT.upper()],
            "validators.powers:" + REPLACEMENT.upper(): 25,
        },
        {"validators.active_validators": [VALIDATOR]},
    )
    assert update_values(ValidatorHandler(app).build_validator_updates(1)) == [
        (REPLACEMENT, 25),
        (VALIDATOR, 0),
    ]
    assert app.client.raw_driver.get(CONSENSUS_KEY) == {REPLACEMENT: 25}


def test_case_only_identity_change_does_not_remove_the_same_consensus_key():
    app = _FakeApp(
        {"validators.active_validators": [VALIDATOR.upper()]},
        {"validators.active_validators": [VALIDATOR]},
    )
    assert ValidatorHandler(app).build_validator_updates(1) == []


@pytest.mark.parametrize(
    "aliases", [[VALIDATOR, VALIDATOR.upper()], [VALIDATOR.upper(), VALIDATOR]]
)
def test_case_aliases_with_equal_power_produce_one_consensus_update(aliases):
    app = _FakeApp({"validators.active_validators": aliases}, {"validators.active_validators": []})
    assert update_values(ValidatorHandler(app).build_validator_updates(1)) == [(VALIDATOR, 10)]


@pytest.mark.parametrize(
    "aliases", [[VALIDATOR, VALIDATOR.upper()], [VALIDATOR.upper(), VALIDATOR]]
)
def test_conflicting_case_alias_powers_preserve_previous_effective_set(aliases):
    app = _FakeApp(
        {
            "validators.active_validators": aliases,
            "validators.powers:" + VALIDATOR: 10,
            "validators.powers:" + VALIDATOR.upper(): 25,
        },
        {"validators.active_validators": [REPLACEMENT]},
    )
    assert ValidatorHandler(app).build_validator_updates(1) == []
    assert app.client.raw_driver.get(CONSENSUS_KEY) == {REPLACEMENT: 10}


@pytest.mark.parametrize("desired", [[], ["con_registrar", 7], "not a validator list", 7])
def test_empty_or_invalid_set_retains_consensus_baseline_across_commit_and_recovery(desired):
    app = _FakeApp(
        {"validators.active_validators": desired},
        {"validators.active_validators": [VALIDATOR]},
    )
    driver = app.client.raw_driver
    assert ValidatorHandler(app).build_validator_updates(1) == []
    assert driver.get(CONSENSUS_KEY) == {VALIDATOR: 10}
    driver.commit()
    # A new handler must not use the now-invalid committed contract list as
    # CometBFT's effective set or forget to remove the retained incumbent.
    driver.set("validators.active_validators", [REPLACEMENT])
    assert update_values(ValidatorHandler(app).build_validator_updates(2)) == [
        (REPLACEMENT, 10),
        (VALIDATOR, 0),
    ]


def test_uncommitted_baseline_does_not_change_repeated_finalize_results():
    app = _FakeApp(
        {"validators.active_validators": [REPLACEMENT]},
        {"validators.active_validators": [VALIDATOR]},
    )
    first = update_values(ValidatorHandler(app).build_validator_updates(1))
    assert first == update_values(ValidatorHandler(app).build_validator_updates(1))
    app.client.raw_driver.commit()
    assert ValidatorHandler(app).build_validator_updates(2) == []


@pytest.mark.parametrize(("power", "expected"), [(0, 10), (-1, 10), (1, 1)])
def test_power_boundary_preserves_default_and_minimum_positive_power(power, expected):
    app = _FakeApp(
        {"validators.active_validators": [VALIDATOR], "validators.powers:" + VALIDATOR: power},
        {"validators.active_validators": []},
    )
    assert update_values(ValidatorHandler(app).build_validator_updates(1)) == [
        (VALIDATOR, expected)
    ]
