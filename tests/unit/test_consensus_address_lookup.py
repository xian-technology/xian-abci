import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

from xian.methods._evidence import _resolve_misbehaving_validator_key
from xian.validator_keys import ValidatorSuite

KEY = "12" * 32
ADDRESS = hashlib.sha256(bytes.fromhex(KEY)).digest()[:20]


def app_with_reader(read):
    return SimpleNamespace(
        client=SimpleNamespace(raw_driver=SimpleNamespace(get=read)),
        validator_handler=SimpleNamespace(suite=ValidatorSuite.ED25519),
    )


def test_historical_evidence_uses_a_direct_consensus_address_index():
    state = {f"validators.consensus_keys:{ADDRESS.hex()}": KEY}
    read = Mock(side_effect=lambda key: state.get(key))
    app = app_with_reader(read)

    assert _resolve_misbehaving_validator_key(app, ADDRESS) == KEY
    read.assert_called_once_with(f"validators.consensus_keys:{ADDRESS.hex()}")


def test_liveness_only_reads_the_bounded_active_set_not_lifetime_registry():
    from xian.methods._evidence import active_validator_keys_by_address

    def read(key):
        assert key == "validators.active_validators"
        return [None, "bad", KEY]

    app = app_with_reader(read)
    assert active_validator_keys_by_address(app) == {ADDRESS: KEY}


def test_an_inconsistent_address_index_cannot_penalize_another_identity():
    read = Mock(return_value="34" * 32)
    app = app_with_reader(read)
    assert _resolve_misbehaving_validator_key(app, ADDRESS) is None
    assert _resolve_misbehaving_validator_key(app, b"") is None
