from copy import deepcopy
from hashlib import sha256
import shutil

import pytest

from tendermint.abci.types_pb2 import RequestInitChain, ValidatorUpdate
from tendermint.crypto.keys_pb2 import PublicKey
from xian.node_setup import generate_validator_material
from xian.genesis_builder import build_validator_genesis_entry_from_public_key
from xian.validator_keys import validate_genesis_validator_keys, validate_init_validator_request


def hardened_genesis():
    member = "01" * 32
    key = "03" * 1952
    address = sha256(bytes.fromhex(key)).hexdigest()[:40]
    entries = {
        "validators.consensus_key_type": "ml_dsa_65",
        "validators.active_validators": [member],
        f"validators.consensus_public_keys:{member}": key,
        f"validators.consensus_keys:{address}": member,
        f"validators.powers:{member}": 9,
    }
    return {
        "chain_id": "local-pq",
        "validators": [
            build_validator_genesis_entry_from_public_key(
                public_key_hex=key, power=9, key_type="ml_dsa_65"
            )
        ],
        "abci_genesis": {"genesis": [{"key": k, "value": v} for k, v in entries.items()]},
        "consensus_params": {
            "validator": {"pub_key_types": ["ml_dsa_65"]},
            "block": {"max_bytes": "22020096"},
            "evidence": {"max_bytes": "1048576"},
        },
    }


def test_hardened_genesis_and_init_chain_bind_keys_powers_and_suite():
    g = hardened_genesis()
    validate_genesis_validator_keys(g)
    request = RequestInitChain(chain_id=g["chain_id"])
    request.consensus_params.validator.pub_key_types.append("ml_dsa_65")
    request.validators.extend(
        [ValidatorUpdate(pub_key=PublicKey(mldsa65=bytes.fromhex("03" * 1952)), power=9)]
    )
    validate_init_validator_request(request, g)
    request.validators[0].power = 8
    with pytest.raises(ValueError):
        validate_init_validator_request(request, g)
    request.validators[0].power = 9
    request.consensus_params.validator.pub_key_types.append("ed25519")
    with pytest.raises(ValueError):
        validate_init_validator_request(request, g)


@pytest.mark.parametrize(
    "bad",
    [
        "empty",
        "mixed",
        "duplicate",
        "ed-only",
        "wrong-address",
        "wrong-key",
        "wrong-length",
        "wrong-power",
        "missing-policy",
        "block-too-small",
    ],
)
def test_hardened_genesis_refuses_unusable_or_downgraded_set(bad):
    g = hardened_genesis()
    entries, params = g["validators"], g["consensus_params"]
    if bad == "empty":
        entries.clear()
    elif bad == "mixed":
        params["validator"]["pub_key_types"].append("ed25519")
    elif bad == "duplicate":
        entries.append(deepcopy(entries[0]))
    elif bad == "ed-only":
        params["validator"]["pub_key_types"] = ["ed25519"]
    elif bad == "wrong-address":
        entries[0]["address"] = "AB" * 20
    elif bad == "wrong-key":
        entries[0]["pub_key"]["type"] = "tendermint/PubKeyEd25519"
    elif bad == "wrong-length":
        entries[0]["pub_key"]["value"] = ""
    elif bad == "wrong-power":
        entries[0]["power"] = "8"
    elif bad == "missing-policy":
        del g["abci_genesis"]["genesis"][0]
    elif bad == "block-too-small":
        params["block"]["max_bytes"] = "1000000"
    with pytest.raises(ValueError):
        validate_genesis_validator_keys(g)


@pytest.mark.skipif(
    shutil.which("xian-validator-key") is None, reason="requires pinned CometBFT key helper"
)
def test_comet_helper_returns_reproducible_fips_filepv_key():
    from base64 import b64decode

    material = generate_validator_material("11" * 32, key_type="ml_dsa_65")
    key = material["priv_validator_key"]
    assert material == generate_validator_material("11" * 32, key_type="ml_dsa_65")
    assert len(b64decode(key["priv_key"]["value"])) == 4032
    assert key["priv_key"]["type"] == "cometbft/PrivKeyMlDsa65"
    assert len(bytes.fromhex(material["validator_public_key_hex"])) == 1952
    assert (
        key["address"]
        == sha256(bytes.fromhex(material["validator_public_key_hex"])).hexdigest()[:40].upper()
    )


def test_handwritten_strict_genesis_requires_pq_validator_suite_and_custody():
    g = hardened_genesis()
    state = g["abci_genesis"]["genesis"]
    state.append({"key": "_accounts.policy", "value": {"version": 2, "transition_chi": 1000}})
    with pytest.raises(ValueError, match="custody"):
        validate_genesis_validator_keys(g)
    g["consensus_params"]["validator"]["pub_key_types"] = ["ed25519"]
    with pytest.raises(ValueError, match="strict"):
        validate_genesis_validator_keys(g)


def test_genesis_rewards_cannot_override_validator_bindings():
    g = hardened_genesis()
    g["abci_genesis"]["rewards"] = [{"key": "validators.consensus_key_type", "value": None}]
    with pytest.raises(ValueError, match="override validator state"):
        validate_genesis_validator_keys(g)


def test_unlimited_block_still_reserves_space_for_commit_and_evidence():
    g = hardened_genesis()
    g["consensus_params"]["block"]["max_bytes"] = "-1"
    g["consensus_params"]["evidence"]["max_bytes"] = "104857600"
    with pytest.raises(ValueError, match="maximum ML-DSA commit"):
        validate_genesis_validator_keys(g)


def test_hardened_genesis_cannot_install_another_committed_validator_baseline():
    g = hardened_genesis()
    g["abci_genesis"]["genesis"].append(
        {"key": "validators.__consensus__", "value": {"02" * 32: 9}}
    )
    with pytest.raises(ValueError, match="committed validator"):
        validate_genesis_validator_keys(g)
