"""Committed node-startup fixtures track the current execution policy and root."""

import json
from pathlib import Path

import pytest
from xian_runtime_types.encoding import convert_dict

from xian.execution_policy import validate_genesis_execution_policy
from xian.state_root import StateRootCache

FIXTURES = sorted(
    Path(__file__).resolve().parents[1].glob("*/fixtures/.cometbft-fixture/config/genesis.json")
)


def test_current_genesis_fixture_discovery_includes_hidden_cometbft_homes():
    assert {path.parents[3].name for path in FIXTURES} == {
        "abci_methods",
        "governance",
        "integration",
    }


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.parents[3].name)
def test_current_genesis_fixture_policy_and_canonical_hash(path):
    genesis = json.loads(path.read_text())
    validate_genesis_execution_policy(genesis)
    state = genesis["abci_genesis"]
    root = StateRootCache(
        (entry["key"], convert_dict(entry["value"])) for entry in state["genesis"]
    ).root_hash.hex()
    assert state["hash"] == root
    assert genesis["app_hash"] == root
