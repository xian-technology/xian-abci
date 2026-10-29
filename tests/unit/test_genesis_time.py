"""Genesis header, deployment metadata and constructors share one UTC instant."""

import json
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from xian_accounts import Ed25519Account
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

from xian.genesis_builder import (
    build_bundle_network_genesis,
    build_cometbft_genesis,
    build_genesis_block,
    build_local_network_genesis,
    build_single_validator_genesis,
)

SEED = "11" * 32
PUBLIC_KEY = Ed25519Account(SEED).public_key
INSTANT = "2026-01-02T03:04:05.123456Z"
EXPECTED_TIME = Datetime(2026, 1, 2, 3, 4, 5, 123456)
VALIDATOR_KEY = {
    "address": "A" * 40,
    "pub_key": {"type": "tendermint/PubKeyEd25519", "value": "validator"},
}


@pytest.fixture
def contracts_dir(tmp_path):
    (tmp_path / "validators.s.py").write_text(
        "created = Variable()\n"
        "@construct\n"
        "def seed(genesis_nodes: list, genesis_registration_fee: int):\n"
        "    created.set(now)\n"
        "@export\n"
        "def get_created():\n"
        "    return created.get()\n",
        encoding="utf-8",
    )
    (tmp_path / "contracts_local.json").write_text(
        json.dumps(
            {
                "extension": ".s.py",
                "contracts": [
                    {
                        "name": "validators",
                        "constructor_args": {
                            "genesis_nodes": [PUBLIC_KEY],
                            "genesis_registration_fee": 1,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def build_network(kind, contracts_dir, **kwargs):
    common = {"chain_id": "xian-local-1", "contracts_dir": contracts_dir, **kwargs}
    if kind == "bundle":
        return build_bundle_network_genesis(network="local", **common)
    if kind == "single":
        return build_single_validator_genesis(
            founder_private_key=SEED, priv_validator_key=VALIDATOR_KEY, **common
        )
    return build_local_network_genesis(
        founder_private_key=SEED,
        validators=[{"account_public_key": PUBLIC_KEY, "priv_validator_key": VALIDATOR_KEY}],
        **common,
    )


def assert_state_time(block, expected):
    state = {entry["key"]: entry["value"] for entry in block["genesis"]}
    assert state["submission.__submitted__"] == expected
    assert state["validators.__submitted__"] == expected
    assert state["validators.created"] == expected


@pytest.mark.parametrize("kind", ["bundle", "single", "local"])
def test_explicit_genesis_time_controls_header_metadata_and_constructor(kind, contracts_dir):
    genesis = build_network(kind, contracts_dir, genesis_time="2026-01-02T05:04:05.123456+02:00")
    assert genesis["genesis_time"] == INSTANT
    assert_state_time(genesis["abci_genesis"], EXPECTED_TIME)


def test_explicit_genesis_is_reproducible_despite_wall_clock_and_timezone(
    contracts_dir, monkeypatch
):
    results = []
    try:
        with monkeypatch.context() as environment:
            for zone, year in [("UTC", 2026), ("Pacific/Honolulu", 2031)]:
                environment.setenv("TZ", zone)
                time.tzset()
                with patch("xian.genesis_builder.datetime", wraps=datetime) as clock:
                    clock.now.return_value = datetime(year, 8, 9, tzinfo=timezone.utc)
                    clock.today.return_value = datetime(year, 8, 8, 14)
                    genesis = build_network("bundle", contracts_dir, genesis_time=INSTANT)
                results.append(encode(genesis))
    finally:
        time.tzset()
    assert results[0] == results[1]


@pytest.mark.parametrize("kind", ["bundle", "single", "local"])
def test_default_genesis_time_is_sampled_once_for_the_whole_build(kind, contracts_dir):
    with patch("xian.genesis_builder.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
        genesis = build_network(kind, contracts_dir)
    clock.now.assert_called_once_with(timezone.utc)
    assert genesis["genesis_time"] == INSTANT
    assert_state_time(genesis["abci_genesis"], EXPECTED_TIME)


@pytest.mark.parametrize("storage", [None, "persistent"])
def test_standalone_genesis_block_uses_explicit_time(contracts_dir, tmp_path, storage):
    block = build_genesis_block(
        founder_private_key=SEED,
        network="local",
        contracts_dir=contracts_dir,
        genesis_time=INSTANT,
        storage_home=tmp_path / storage if storage else None,
    )
    assert_state_time(block, EXPECTED_TIME)


@pytest.mark.parametrize("value", ["2026-01-02T03:04:05", "2026-01-02", "not-a-date"])
def test_genesis_rejects_timestamps_without_an_explicit_timezone(value, contracts_dir):
    with pytest.raises(ValueError):
        build_network("bundle", contracts_dir, genesis_time=value)
    with pytest.raises(ValueError):
        build_cometbft_genesis(chain_id="xian-local-1", abci_genesis={}, genesis_time=value)


def test_standalone_comet_genesis_normalizes_timezone():
    genesis = build_cometbft_genesis(
        chain_id="xian-local-1", abci_genesis={}, genesis_time="2026-01-02T05:04:05.123456+02:00"
    )
    assert genesis["genesis_time"] == INSTANT
