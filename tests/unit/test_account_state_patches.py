from pathlib import Path

import pytest

from xian.utils.state_patches import _validate_patch_key


@pytest.mark.parametrize("key", ["_accounts.records:" + "ab" * 32, "_accounts.__source__"])
def test_governed_patches_cannot_replace_account_authority(key):
    with pytest.raises(ValueError, match="account registry"):
        _validate_patch_key(Path("governed.json"), key)


def test_genesis_records_require_policy_and_strict_genesis_disables_zk():
    from xian.execution_policy import current_execution_policy, validate_genesis_execution_policy

    execution = {"key": "__execution_policy", "value": current_execution_policy()}
    other = {"key": "_accounts.records:" + "ab" * 32, "value": {}}
    with pytest.raises(ValueError):
        validate_genesis_execution_policy({"genesis": [execution, other]})
    strict = {"key": "_accounts.policy", "value": {"version": 2, "transition_chi": 1000}}
    zk = {"key": "__runtime_features.zk", "value": True}
    with pytest.raises(ValueError, match="zk"):
        validate_genesis_execution_policy({"genesis": [execution, strict, zk]})
    with pytest.raises(ValueError, match="account"):
        validate_genesis_execution_policy({"genesis": [execution, strict], "rewards": [other]})


def test_local_genesis_refuses_unprotected_founder_before_deploying_contracts():
    from nacl.signing import SigningKey
    from xian.genesis_builder import build_local_network_genesis
    from xian_accounts import hybrid
    import base64

    public, _ = hybrid.backend().generate_keypair()
    with pytest.raises(ValueError, match="custody"):
        build_local_network_genesis(
            chain_id="strict-local",
            network="local",
            founder_private_key=bytes(SigningKey.generate()).hex(),
            validators=[
                {
                    "account_public_key": "44" * 32,
                    "priv_validator_key": {
                        "pub_key": {
                            "type": "cometbft/PubKeyMlDsa65",
                            "value": base64.b64encode(public).decode(),
                        }
                    },
                }
            ],
            account_policy={"version": 2, "transition_chi": 1000},
            account_records={},
        )


def test_strict_cutover_and_validator_authority_require_a_network_upgrade():
    from unittest.mock import Mock
    from xian.utils.state_patches import _validate_authority_changes

    strict = {"version": 2, "transition_chi": 1000}
    read = Mock(return_value=None)
    with pytest.raises(ValueError, match="network upgrade"):
        _validate_authority_changes(read, {"_accounts.policy": strict})
    read.side_effect = lambda contract, variable: strict if contract == "_accounts" else None
    for key in (
        "validators.consensus_key_type",
        "validators.consensus_public_keys:alice",
        "validators.consensus_keys:hash",
        "validator_key_proofs.__source__",
        "validators.__source__",
        "validators.active_validators",
        "validators.powers:alice",
    ):
        with pytest.raises(ValueError, match="validator authority"):
            _validate_authority_changes(read, {key: None})
    with pytest.raises(ValueError, match="legacy"):
        _validate_authority_changes(
            read,
            {"_accounts.policy": {"version": 1, "enrollment_open": True, "transition_chi": 1000}},
        )


def test_strict_account_restart_requires_its_committed_policy(node_constants):
    from contracting.storage.driver import Driver
    from xian.account_registry import RegistryPolicy, validate_persisted_account_policy

    policy = RegistryPolicy.parse({"version": 2, "transition_chi": 1000})
    with Driver(vm_profile="xian_vm_v2", storage_home=node_constants.STORAGE_HOME) as driver:
        driver.set(node_constants.LATEST_BLOCK_KEY, {"height": 1})
        driver.commit()
        with pytest.raises(ValueError, match="legacy"):
            validate_persisted_account_policy(driver, policy, node_constants.LATEST_BLOCK_KEY)


def test_transition_must_fit_the_genesis_transaction_budget():
    from xian.execution_policy import current_execution_policy, validate_genesis_execution_policy

    state = {
        "genesis": [
            {"key": "__execution_policy", "value": current_execution_policy()},
            {
                "key": "__fee_policy",
                "value": {"tx_fee_mode": "paid_metered", "max_tx_chi": 2, "max_block_chi": 50},
            },
            {
                "key": "_accounts.policy",
                "value": {"version": 1, "enrollment_open": False, "transition_chi": 3},
            },
        ]
    }
    with pytest.raises(ValueError, match="transaction chi cap"):
        validate_genesis_execution_policy(state)
