"""A recovery genesis restarts a chain halted by offline voting power."""

import json

import pytest
from xian_runtime_types.time import Datetime

from xian.evidence_policy import EVIDENCE_POLICY_KEY, EvidencePolicy
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
        EVIDENCE_POLICY_KEY: EvidencePolicy.seed(7, nanos=1_700_000_000_000_000_000).to_state(),
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
        # xian-export-state's form: the stored key without the "__n." prefix.
        "nonces": [{"key": "alice:", "value": 3}],
    }
    state["hash"] = compute_exported_state_root(state).hex()
    return state


def _trusted_checkpoint(height=461):
    # Independent fixture checkpoint, captured from the original untampered state.
    original = _exported(height)
    return {
        "schema_version": 1,
        "height": str(height),
        "app_hash": original["hash"],
        "last_block_nanos": "1700000000000000000",
    }


def _state(genesis):
    return {entry["key"]: entry["value"] for entry in genesis["abci_genesis"]["genesis"]}


def test_the_online_validators_continue_the_chain_alone():
    genesis = build_recovery_genesis(
        _exported(),
        ONLINE,
        trusted_checkpoint=_trusted_checkpoint(),
        chain_id="xian-test-1",
        genesis_time=GENESIS_TIME,
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
    assert genesis["abci_genesis"]["nonces"] == [{"key": "alice:", "value": 3}]


def test_the_recovery_genesis_skips_the_halted_height_with_a_valid_root():
    genesis = build_recovery_genesis(
        _exported(461),
        ONLINE,
        trusted_checkpoint=_trusted_checkpoint(),
        chain_id="xian-test-1",
        genesis_time=GENESIS_TIME,
    )

    abci_genesis = genesis["abci_genesis"]
    assert abci_genesis["number"] == 461
    # The halted chain voted on 462 and never committed it. Starting at 463
    # leaves no height both chains signed: no duplicate-vote evidence from
    # the halted chain's votes, and no validator waits for its signer's
    # last round.
    assert genesis["initial_height"] == "463"
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
        build_recovery_genesis(
            _exported(),
            keep,
            trusted_checkpoint=_trusted_checkpoint(),
            chain_id="c",
            genesis_time=GENESIS_TIME,
        )


def test_a_genesis_time_before_the_last_block_is_refused():
    # 1_700_000_000 s is 2023-11-14T22:13:20Z: contract time must not go back.
    with pytest.raises(ValueError, match="after the last block"):
        build_recovery_genesis(
            _exported(),
            ONLINE,
            trusted_checkpoint=_trusted_checkpoint(),
            chain_id="c",
            genesis_time="2023-11-14T22:13:20.000000Z",
        )


def test_a_tampered_export_is_refused():
    exported = _exported()
    [balance] = [e for e in exported["genesis"] if e["key"] == "currency.balances:alice"]
    balance["value"] = 6

    with pytest.raises(ValueError, match="state root"):
        build_recovery_genesis(
            exported,
            ONLINE,
            trusted_checkpoint=_trusted_checkpoint(),
            chain_id="c",
            genesis_time=GENESIS_TIME,
        )


def test_the_cli_writes_the_same_genesis_for_every_operator(tmp_path):
    from xian_runtime_types.encoding import encode

    from xian.cli.recovery_genesis import main

    exported = tmp_path / "exported_state.json"
    exported.write_text(encode(_exported(), vm_profile="xian_vm_v2"))
    checkpoint = tmp_path / "trusted-checkpoint.json"
    checkpoint.write_text(json.dumps(_trusted_checkpoint()))
    outputs = []
    for operator in ("a", "b"):
        output = tmp_path / operator / "genesis.json"
        args = [
            "--exported-state",
            str(exported),
            "--trusted-checkpoint",
            str(checkpoint),
            "--chain-id",
            "xian-test-1",
        ]
        args += ["--genesis-time", GENESIS_TIME, "--output", str(output)]
        for key in ONLINE:
            args += ["--keep", key]
        assert main(args) == 0
        outputs.append(output.read_bytes())

    assert outputs[0] == outputs[1]


def test_a_node_starting_from_the_written_genesis_reaches_its_app_hash(tmp_path):
    """InitChain applies the genesis file and refuses it unless the state it
    builds has the advertised root; the senders' nonces must survive, or
    their committed transactions could be replayed on the recovered chain."""
    import asyncio

    from contracting.local import ContractingClient
    from xian_runtime_types.encoding import loads_wire_json

    from xian.genesis_builder import write_genesis_block
    from xian.nonce import NonceStorage
    from xian.state_root import StateRootCache
    from xian.utils.block import store_genesis_block

    path = tmp_path / "genesis.json"
    write_genesis_block(
        path,
        build_recovery_genesis(
            _exported(),
            ONLINE,
            trusted_checkpoint=_trusted_checkpoint(),
            chain_id="c",
            genesis_time=GENESIS_TIME,
        ),
    )
    genesis = loads_wire_json(path.read_bytes())
    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=tmp_path / "home")
    try:
        client.raw_driver.flush_full()
        asyncio.run(store_genesis_block(client, genesis["abci_genesis"]))
        root = StateRootCache.from_driver(client.raw_driver).root_hash.hex()
        assert root == genesis["app_hash"]
        assert NonceStorage(client).get_nonce("alice") == 3
    finally:
        client.close()


def test_recovery_cli_requires_the_independent_checkpoint_before_writing(tmp_path):
    from xian.cli.recovery_genesis import main

    output = tmp_path / "genesis.json"
    args = [
        "--exported-state",
        str(tmp_path / "untrusted.json"),
        "--keep",
        ONLINE[0],
        "--chain-id",
        "c",
        "--genesis-time",
        GENESIS_TIME,
        "--output",
        str(output),
    ]
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert not output.exists()
