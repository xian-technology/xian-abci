"""Composing genesis in stages preserves deployment and finalization semantics."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from contracting.storage.driver import Driver
from xian_accounts import Ed25519Account
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

from xian.genesis_builder import (
    build_genesis_block,
    deploy_bundle_contracts,
    finalize_genesis_block,
    hash_state_changes,
    normalize_genesis_time,
    parse_genesis_time,
)
from xian.state_root import StateRootCache

SEED = "11" * 32
INSTANT = "2026-01-02T05:04:05.123456+02:00"


@pytest.fixture
def bundle(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "clock.s.py").write_text(
        "created = Variable()\n"
        "founder = Variable()\n"
        "amount = Variable()\n"
        "@construct\n"
        "def seed(account: str, value: int):\n"
        "    created.set(now)\n"
        "    founder.set(account)\n"
        "    amount.set(value)\n"
        "@export\n"
        "def read():\n"
        "    return amount.get()\n",
        encoding="utf-8",
    )
    (contracts / "contracts_fixture.json").write_text(
        json.dumps(
            {
                "extension": ".s.py",
                "contracts": [
                    {
                        "name": "clock",
                        "submit_as": "clock_state",
                        "constructor_args": {
                            "account": "%%founder_public_key%%",
                            "value": 1,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "founder_private_key": SEED,
        "network": "fixture",
        "contracts_dir": contracts,
        "constructor_overrides": {"clock_state": {"value": 17}},
        "runtime_features": {"zk": False},
    }


def test_staged_deployment_matches_existing_builder(bundle, tmp_path):
    expected = build_genesis_block(**bundle, genesis_time=INSTANT)
    with Driver(storage_home=tmp_path / "staged") as driver:
        deploy_bundle_contracts(driver, **bundle, submission_time=parse_genesis_time(INSTANT))
        assert driver.pending_writes
        assert driver._store.items() == {}  # Deployment has not committed to storage.
        result = finalize_genesis_block(driver, founder_private_key=SEED)
        assert not driver.pending_writes
        assert encode(result) == encode(expected)
        state = {entry["key"]: entry["value"] for entry in result["genesis"]}
        for key in ("submission.__submitted__", "clock_state.__submitted__", "clock_state.created"):
            assert state[key] == Datetime(2026, 1, 2, 3, 4, 5, 123456)
        assert state["clock_state.amount"] == 17
        assert state["clock_state.founder"] == Ed25519Account(SEED).public_key


@pytest.mark.parametrize("signer", [SEED, None])
def test_finalize_includes_layered_state_and_optional_signature(bundle, tmp_path, signer):
    with Driver(storage_home=tmp_path / "layered") as driver:
        deploy_bundle_contracts(driver, **bundle, submission_time=parse_genesis_time(INSTANT))
        driver.set("clock_state.amount", 23)
        driver.set("extra.state", {"items": [1, 2]})
        result = finalize_genesis_block(driver, founder_private_key=signer, number=42)
        assert result["number"] == "42"
        assert result["genesis"] == [
            {"key": key, "value": value} for key, value in sorted(driver.items().items())
        ]
        assert driver.get("clock_state.amount") == 23
        assert driver.get("extra.state") == {"items": [1, 2]}
        assert result["hash"] == StateRootCache.from_driver(driver).root_hash.hex()
        if signer is None:
            assert result["origin"] == {"sender": "", "signature": ""}
        else:
            account = Ed25519Account(signer)
            assert result["origin"]["sender"] == account.public_key
            assert account.verify_msg(
                hash_state_changes(result["genesis"]), result["origin"]["signature"]
            )


def test_public_timestamp_helpers_normalize_offsets():
    assert normalize_genesis_time(INSTANT) == "2026-01-02T03:04:05.123456Z"
    assert parse_genesis_time(INSTANT) == Datetime(2026, 1, 2, 3, 4, 5, 123456)


def test_staged_default_time_is_sampled_once_and_existing_contract_state_is_kept(bundle, tmp_path):
    with Driver(storage_home=tmp_path / "default-time") as driver:
        with patch("xian.genesis_builder.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 1, 2, tzinfo=timezone.utc)
            deploy_bundle_contracts(driver, **bundle)
            clock.now.assert_called_once_with(timezone.utc)
        for key in ("submission.__submitted__", "clock_state.__submitted__", "clock_state.created"):
            assert driver.get(key) == Datetime(2026, 1, 2)
        driver.set("clock_state.amount", 99)
        deploy_bundle_contracts(driver, **bundle, submission_time=parse_genesis_time(INSTANT))
        result = finalize_genesis_block(driver, founder_private_key=SEED)
        state = {entry["key"]: entry["value"] for entry in result["genesis"]}
        assert state["clock_state.amount"] == 99
        assert state["clock_state.created"] == Datetime(2026, 1, 2)


def test_constructor_overrides_can_supply_missing_args_by_source_name(bundle, tmp_path):
    config_path = bundle["contracts_dir"] / "contracts_fixture.json"
    config = json.loads(config_path.read_text())
    del config["contracts"][0]["constructor_args"]
    config_path.write_text(json.dumps(config))
    bundle["constructor_overrides"] = {"clock": {"account": "custom", "value": 29}}
    with Driver(storage_home=tmp_path / "override") as driver:
        deploy_bundle_contracts(driver, **bundle, submission_time=parse_genesis_time(INSTANT))
        result = finalize_genesis_block(driver, founder_private_key=SEED)
        state = {entry["key"]: entry["value"] for entry in result["genesis"]}
        assert state["clock_state.amount"] == 29
        assert state["clock_state.founder"] == "custom"


@pytest.mark.parametrize("value", ["2026-01-02T03:04:05", "2026-01-02", "invalid"])
def test_public_timestamp_helpers_reject_invalid_or_naive_values(value):
    with pytest.raises(ValueError):
        normalize_genesis_time(value)
    with pytest.raises(ValueError):
        parse_genesis_time(value)
