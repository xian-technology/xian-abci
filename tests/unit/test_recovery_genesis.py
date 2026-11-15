"""A recovery genesis restarts a chain halted by offline voting power."""

import pytest
from xian_runtime_types.time import Datetime

from xian.execution_policy import EXECUTION_POLICY_KEY, current_execution_policy
from xian.recovery_genesis import build_recovery_genesis
from xian.state_root import compute_exported_state_root

ONLINE = ["11" * 32, "22" * 32, "33" * 32]
OFFLINE = ["44" * 32, "55" * 32]
GENESIS_TIME = "2026-11-16T10:00:00.000000Z"


def _exported(height=461):
    active = ONLINE + OFFLINE
    entries = {
        "currency.balances:alice": 5,
        "validators.active_validators": active,
        "validators.__consensus__": {key: 10 for key in active},
        "validators.config:selection_mode": "auto_top_n",
        EXECUTION_POLICY_KEY: current_execution_policy(),
    }
    for key in active:
        entries[f"validators.powers:{key}"] = 10
        entries[f"validators.statuses:{key}"] = "active"
    state = {
        "hash": "",
        "number": height,
        "nanos": 1_700_000_000_000_000_000,
        "origin": {"signature": "", "sender": ""},
        "genesis": [{"key": key, "value": value} for key, value in sorted(entries.items())],
        "nonces": [{"key": "alice", "value": 3}],
    }
    state["hash"] = compute_exported_state_root(state).hex()
    return state


def _state(genesis):
    return {entry["key"]: entry["value"] for entry in genesis["abci_genesis"]["genesis"]}


def test_the_online_validators_continue_the_chain_alone():
    genesis = build_recovery_genesis(
        _exported(), ONLINE, chain_id="xian-test-1", genesis_time=GENESIS_TIME
    )

    state = _state(genesis)
    assert state["validators.active_validators"] == ONLINE
    assert state["validators.__consensus__"] == {key: 10 for key in ONLINE}
    for key in OFFLINE:
        assert state[f"validators.powers:{key}"] == 0
        assert state[f"validators.statuses:{key}"] == "approved"
        assert state[f"validators.jailed:{key}"] is True
        # They come back as validators jailed for downtime do.
        assert state[f"validators.jail_reasons:{key}"] == "downtime"
        assert state[f"validators.last_jailed_at:{key}"] == Datetime(2026, 11, 16, 10)
    assert state["currency.balances:alice"] == 5
    assert genesis["abci_genesis"]["nonces"] == [{"key": "alice", "value": 3}]


def test_the_recovery_genesis_continues_at_the_next_height_with_a_valid_root():
    genesis = build_recovery_genesis(
        _exported(461), ONLINE, chain_id="xian-test-1", genesis_time=GENESIS_TIME
    )

    abci_genesis = genesis["abci_genesis"]
    assert abci_genesis["number"] == 461
    assert genesis["initial_height"] == "462"
    assert bytes.fromhex(abci_genesis["hash"]) == compute_exported_state_root(abci_genesis)
    assert genesis["app_hash"] == abci_genesis["hash"]
    assert genesis["chain_id"] == "xian-test-1"
    assert genesis["genesis_time"] == GENESIS_TIME
    assert [entry["power"] for entry in genesis["validators"]] == ["10", "10", "10"]
    assert len(genesis["validators"]) == 3


@pytest.mark.parametrize(
    "keep,message",
    [([], "at least one"), (["66" * 32], "not active"), (ONLINE + ONLINE[:1], "duplicate")],
)
def test_the_validators_kept_must_be_distinct_active_validators(keep, message):
    with pytest.raises(ValueError, match=message):
        build_recovery_genesis(_exported(), keep, chain_id="c", genesis_time=GENESIS_TIME)


def test_a_tampered_export_is_refused():
    exported = _exported()
    [balance] = [e for e in exported["genesis"] if e["key"] == "currency.balances:alice"]
    balance["value"] = 6

    with pytest.raises(ValueError, match="state root"):
        build_recovery_genesis(exported, ONLINE, chain_id="c", genesis_time=GENESIS_TIME)


def test_the_cli_writes_the_same_genesis_for_every_operator(tmp_path):
    from xian_runtime_types.encoding import encode

    from xian.cli.recovery_genesis import main

    exported = tmp_path / "exported_state.json"
    exported.write_text(encode(_exported(), vm_profile="xian_vm_v2"))
    outputs = []
    for operator in ("a", "b"):
        output = tmp_path / operator / "genesis.json"
        args = ["--exported-state", str(exported), "--chain-id", "xian-test-1"]
        args += ["--genesis-time", GENESIS_TIME, "--output", str(output)]
        for key in ONLINE:
            args += ["--keep", key]
        assert main(args) == 0
        outputs.append(output.read_bytes())

    assert outputs[0] == outputs[1]
