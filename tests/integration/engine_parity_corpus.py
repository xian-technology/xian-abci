"""Golden corpora for moving the per-transaction rules into the Rust engine.

The single-engine plan (xian-meta ``docs/XIAN_SINGLE_ENGINE_PLAN.md``) moves
every transaction rule that the Python per-transaction path still owns into
the Rust executor and then deletes that path. These corpora cover what moves:
contract submission and deploys, random, contract metadata changes, zk
syscalls, system transactions (epoch rebalance, evidence penalties), values
the receipt encoding may refuse, odd balances, reward inputs the Rust port
leaves to Python, simulation and contract-view queries.

Each corpus runs on two paths:

* the Python path: every transaction through ``TxProcessor.process_tx``
  (``TxProcessor._block_executor`` patched to ``None``; no production flag),
  simulations through ``TransactionSimulator`` in-process, contract views
  through ``execute_vm_contract``;
* the production path: ``TxProcessor.process_block`` (Rust with hand-backs),
  simulations through the ``QuerySimulationService`` worker process, contract
  views through the query handler's ``_call_contract_view``, system
  transactions through ``FinalizeBlock``.

``tests/fixtures/engine_parity/`` freezes the Python-path outputs; see the
README there for when they may be regenerated:

    python -m tests.integration.engine_parity_corpus --write

``handback_inventory.json`` there lists the cases the production path still
hands to the Python path; each increment that moves a rule into Rust removes
its cases (``--write-inventory`` rewrites it from the production path).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from loguru import logger

from xian.utils.tx import transaction_bytes
from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor

if __package__:
    from . import engine_parity_contracts as sources
else:  # imported by pytest with tests/integration on sys.path
    import engine_parity_contracts as sources

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[2]
FIXTURES = HERE.parent / "fixtures" / "engine_parity"
ZK_DEMO = FIXTURES / "inputs" / "groth16_bn254_demo.json"
NODE_FIXTURE = HERE / "fixtures" / ".cometbft-fixture"
CHAIN_ID = "xian-local"
BASE_NANOS = 1_700_000_000_000_000_000

# The route of a transaction that the Rust executor ran, and of one that ran
# on the per-transaction path because the block had no Rust executor.
RUST = "rust"
PER_TX_PATH = "per-transaction path"

POLICIES: dict[str, Callable[[], TxFeePolicy]] = {
    "paid": TxFeePolicy.paid_metered,
    "free": TxFeePolicy.free_metered,
    "unmetered": TxFeePolicy.system_unmetered,
}


@dataclass(frozen=True)
class Call:
    case: str
    sender: str
    contract: str
    function: str
    kwargs: dict
    chi: int = 5_000


@dataclass(frozen=True)
class Block:
    calls: tuple[Call, ...]
    # Writes applied before the block's transactions, as a state patch.
    patch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Corpus:
    name: str
    blocks: tuple[Block, ...]
    policy: str = "paid"


def _deploy(case, sender, name, code, chi=100_000, **extra):
    return Call(
        case, sender, "submission", "submit_contract", {"name": name, "code": code, **extra}, chi
    )


def _fixed(text: str) -> dict:
    return {"__fixed__": text}


# --- chain setup -----------------------------------------------------------

SUBMITTED = (2026, 1, 2, 3, 4, 5)
CONTRACTS = (
    ("con_probe", sources.PROBE, {"developer": "dev"}),
    ("con_rand_caller", sources.RAND_CALLER, {}),
    ("con_factory", sources.FACTORY, {"developer": "dev"}),
    ("con_meta", sources.META, {}),
    ("con_owned", sources.PINGABLE, {"owner": "bob"}),
    ("con_owned_alice", sources.PINGABLE, {"owner": "alice", "developer": "alice"}),
    ("con_unowned", sources.PINGABLE, {"developer": "dev"}),
    ("con_values", sources.VALUES, {}),
    ("con_zk", sources.ZK_PROBE, {}),
    ("con_missing_import", sources.MISSING_IMPORT, {}),
    ("con_zk_importer", sources.ZK_IMPORTER, {}),
    ("validators", sources.VALIDATORS, {}),
    ("rewards", sources.REWARDS, {}),
    ("foundation", sources.FOUNDATION, {}),
    ("zk_registry", sources.ZK_REGISTRY, {}),
)
# Balances by account: int, ContractingDecimal (str), absent (None) and
# values the fee path cannot treat as a number.
BALANCES = {
    "alice": 100_000,
    "bob": "2500.5",
    "carol": None,
    "dave": "0.0003",
    "dev": 1_000,
    "dev2": 1_000,
    "erin": "lots",
    "fay": {"amount": 5},
    "gus": [1, 2],
    "hal": True,
    "ivy": -5,
    "jon": "-1.5",
    "kim": "0.25",
    "lea": "0.25",
    "mia": "101",
    "nia": "101",
}
# Balances stored as the text itself, not as a ContractingDecimal.
LITERAL_BALANCES = {"erin"}


def _zk_demo() -> dict:
    return json.loads(ZK_DEMO.read_text(encoding="utf-8"))


def _fix_submission_time(driver) -> None:
    """ContractingClient stamps its submission contract with the current time."""
    from xian_runtime_types.time import Datetime

    driver.set("submission.__submitted__", Datetime(*SUBMITTED))


def _deploy_contracts(driver) -> None:
    from xian_runtime_types.time import Datetime

    submitted = Datetime(*SUBMITTED)
    _fix_submission_time(driver)
    currency = (WORKSPACE / "xian-configs/contracts/currency.s.py").read_text()
    driver.set_contract_from_source("currency", currency, timestamp=submitted)
    for name, source, metadata in CONTRACTS:
        driver.set_contract_from_source(name, source, timestamp=submitted, **metadata)
    # Contracts the Python path refuses to prepare.
    driver.set_contract_from_source("con_cycle_a", sources.CYCLE_A, timestamp=submitted)
    driver.set_contract_from_source("con_cycle_b", sources.CYCLE_B, timestamp=submitted)
    driver.set("con_source_only.__source__", sources.PINGABLE)
    driver.set("con_bad_ir.__xian_ir_v1__", "not json")
    driver.set("con_int_ir.__xian_ir_v1__", 5)


def _fund_accounts(driver) -> None:
    from xian_runtime_types.decimal import ContractingDecimal

    for account, balance in BALANCES.items():
        if balance is None:
            continue
        if isinstance(balance, str) and account not in LITERAL_BALANCES:
            balance = ContractingDecimal(balance)
        driver.set(f"currency.balances:{account}", balance)
        driver.set(f"currency.approvals:{account}:con_probe", 50)
    driver.set("currency.balances:val_bad", "not a number")


def _configure_chain(driver) -> None:
    """The chi rate, the reward configuration and a registered zk key."""
    from xian_runtime_types.decimal import ContractingDecimal

    driver.set("chi_cost.S:value", 20)
    driver.set("foundation.owner", "foundation")
    driver.set("validators.active_validators", ["val1", "val2"])
    driver.set("validators.powers:val2", 3)
    driver.set(
        "rewards.S:value",
        [ContractingDecimal("0.5"), 0, ContractingDecimal("0.3"), ContractingDecimal("0.2")],
    )
    registered = {"scheme": "groth16", "curve": "bn254", "vk_hash": "0xdemo", "active": True}
    registered["vk_hex"] = _zk_demo()["vk_hex"]
    registered["circuit_name"] = "demo"
    for field_name, value in registered.items():
        driver.set(f"zk_registry.verifying_keys:demo:{field_name}", value)


def open_chain(home: Path):
    from contracting.local import ContractingClient

    from xian.rewards import RewardsHandler

    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=home)
    driver = client.raw_driver
    _deploy_contracts(driver)
    _fund_accounts(driver)
    _configure_chain(driver)
    driver.commit()
    return client, TxProcessor(client=client), RewardsHandler(client)


# --- the corpora -------------------------------------------------------------


def _deploy_corpus() -> Corpus:
    long_name = "con_" + "x" * 61
    return Corpus(
        "deploy",
        (
            Block(
                (
                    _deploy(
                        "deploy/constructor_args",
                        "alice",
                        "con_d1",
                        sources.DEPLOYED,
                        constructor_args={"start": 5},
                    ),
                    Call("deploy/call_in_same_block", "alice", "con_d1", "get", {}),
                    _deploy("deploy/no_constructor", "alice", "con_d2", sources.DEPLOYED_PLAIN),
                    _deploy("deploy/constructor_default", "alice", "con_d4", sources.DEPLOYED),
                    _deploy(
                        "deploy/lint_no_export", "alice", "con_bad1", sources.DEPLOYED_LINT_ERROR
                    ),
                    _deploy(
                        "deploy/lint_forbidden_name",
                        "alice",
                        "con_bad2",
                        sources.DEPLOYED_FORBIDDEN,
                    ),
                    _deploy(
                        "deploy/syntax_error", "alice", "con_bad3", sources.DEPLOYED_SYNTAX_ERROR
                    ),
                    _deploy(
                        "deploy/constructor_fails",
                        "alice",
                        "con_bad4",
                        sources.DEPLOYED_FAILING_CONSTRUCTOR,
                    ),
                    _deploy(
                        "deploy/constructor_bad_arg",
                        "alice",
                        "con_bad5",
                        sources.DEPLOYED,
                        constructor_args={"start": "x"},
                    ),
                    _deploy(
                        "deploy/constructor_unknown_arg",
                        "alice",
                        "con_bad6",
                        sources.DEPLOYED,
                        constructor_args={"nope": 1},
                    ),
                    _deploy("deploy/name_collision", "alice", "con_d1", sources.DEPLOYED),
                    _deploy(
                        "deploy/name_of_system_contract", "alice", "currency", sources.DEPLOYED
                    ),
                    _deploy("deploy/name_without_prefix", "alice", "mine", sources.DEPLOYED),
                    _deploy("deploy/name_uppercase", "alice", "con_Bad", sources.DEPLOYED),
                    _deploy("deploy/name_too_long", "alice", long_name, sources.DEPLOYED),
                    _deploy("deploy/empty_code", "alice", "con_bad7", ""),
                    Call(
                        "deploy/missing_code",
                        "alice",
                        "submission",
                        "submit_contract",
                        {"name": "con_bad8"},
                        100_000,
                    ),
                    _deploy(
                        "deploy/owner", "alice", "con_d5", sources.DEPLOYED_PLAIN, owner="alice"
                    ),
                    _deploy(
                        "deploy/owner_empty", "alice", "con_bad9", sources.DEPLOYED_PLAIN, owner=""
                    ),
                    _deploy(
                        "deploy/constructor_events",
                        "alice",
                        "con_d6",
                        sources.DEPLOYED_EVENTS,
                        constructor_args={"start": 3},
                    ),
                    _deploy(
                        "deploy/out_of_chi", "alice", "con_bad10", sources.DEPLOYED_LARGE, chi=2_000
                    ),
                    _deploy("deploy/large", "alice", "con_d7", sources.DEPLOYED_LARGE),
                    _deploy("deploy/zk_contract", "alice", "con_d8", sources.DEPLOYED_ZK),
                    _deploy(
                        "deploy/imports_deployed", "alice", "con_d3", sources.DEPLOYED_IMPORTER
                    ),
                    _deploy("deploy/unfunded_sender", "carol", "con_bad11", sources.DEPLOYED),
                    Call(
                        "deploy/factory",
                        "alice",
                        "con_factory",
                        "make",
                        {"name": "con_f1", "code": sources.DEPLOYED},
                        100_000,
                    ),
                    Call(
                        "deploy/factory_name_without_prefix",
                        "alice",
                        "con_factory",
                        "make",
                        {"name": "f2", "code": sources.DEPLOYED},
                        100_000,
                    ),
                    Call(
                        "deploy/factory_deploy_then_call",
                        "alice",
                        "con_factory",
                        "make_and_call",
                        {"name": "con_f3", "code": sources.DEPLOYED},
                        100_000,
                    ),
                    Call(
                        "deploy/factory_twice",
                        "alice",
                        "con_factory",
                        "make_twice",
                        {"name": "con_f4", "code": sources.DEPLOYED},
                        100_000,
                    ),
                    Call(
                        "deploy/factory_then_fail",
                        "alice",
                        "con_factory",
                        "make_then_fail",
                        {"name": "con_f5", "code": sources.DEPLOYED},
                        100_000,
                    ),
                    _deploy(
                        "deploy/name_of_rolled_back_deploy",
                        "alice",
                        "con_f5",
                        sources.DEPLOYED_PLAIN,
                    ),
                )
            ),
            Block(
                (
                    Call("deploy/call_previous_block", "bob", "con_d1", "get", {}),
                    Call("deploy/call_factory_deploy", "bob", "con_f1", "get", {}),
                    Call("deploy/call_importer", "bob", "con_d3", "get", {}),
                    Call("deploy/call_constructor_events", "bob", "con_d6", "get", {}),
                    Call("deploy/call_owned_by_owner", "alice", "con_d5", "get", {}),
                    Call("deploy/call_owned_by_other", "bob", "con_d5", "get", {}),
                    Call("deploy/call_failed_deploy", "bob", "con_bad4", "get", {}),
                    Call("deploy/call_rolled_back_factory_deploy", "bob", "con_f4", "get", {}),
                    Call("deploy/info_of_deploy", "bob", "con_meta", "info", {"name": "con_f1"}),
                )
            ),
            # A sender balance the fee path cannot read halts the node.
            Block((_deploy("deploy/str_balance_sender", "erin", "con_bad12", sources.DEPLOYED),)),
        ),
    )


RANDOM_SALTS = {
    "str": "abc",
    "int": 42,
    "negative_int": -7,
    "big_int": {"__big_int__": str(2**80)},
    "none": None,
    "bool": False,
    "decimal": _fixed("1.5"),
    "list": [1, "a"],
    "dict": {"b": 1, "a": 2},
    "empty_str": "",
}


def _random_corpus() -> Corpus:
    salts = tuple(
        Call(f"random/salt_{name}", "alice", "con_probe", "rnd_salt", {"salt": salt})
        for name, salt in RANDOM_SALTS.items()
    )
    internal = tuple(
        Call(
            f"random/internal_salt_{kind}",
            "alice",
            "con_probe",
            "rnd_internal_salt",
            {"kind": kind},
        )
        for kind in ("datetime", "decimal", "tuple", "list", "bool", "dict", "timedelta")
    )
    errors = tuple(
        Call(f"random/error_{kind}", "alice", "con_probe", "rnd_error", {"kind": kind})
        for kind in ("randint", "choice", "choices", "getrandbits", "randrange", "shuffle")
    )
    return Corpus(
        "random",
        (
            Block(
                (
                    Call("random/plain", "alice", "con_probe", "rnd_plain", {}),
                    Call("random/plain_other_signature", "alice", "con_probe", "rnd_plain", {}),
                    *salts,
                    *internal,
                    Call("random/unseeded", "alice", "con_probe", "rnd_unseeded", {}),
                    *errors,
                    Call("random/reseed", "alice", "con_probe", "rnd_reseed", {}),
                    Call(
                        "random/nested_shares_seed", "alice", "con_rand_caller", "outer_seeded", {}
                    ),
                    Call(
                        "random/nested_unseeded", "alice", "con_rand_caller", "outer_unseeded", {}
                    ),
                    Call(
                        "random/nested_seeds_for_caller",
                        "alice",
                        "con_rand_caller",
                        "inner_seeds",
                        {},
                    ),
                    Call("random/state_not_carried_over", "alice", "con_probe", "rnd_draw", {}),
                    Call("random/float", "alice", "con_probe", "rnd_float", {}),
                    Call("random/store_float", "alice", "con_probe", "rnd_store_float", {}),
                    Call("random/then_fail", "alice", "con_probe", "rnd_then_fail", {}),
                    Call("random/low_chi", "alice", "con_probe", "rnd_plain", {}, 60),
                    Call("random/unfunded_sender", "carol", "con_probe", "rnd_plain", {}),
                )
            ),
            Block((Call("random/read_stored_float", "alice", "con_probe", "read_stored", {}),)),
        ),
    )


def _change(case, sender, contract, role, holder, chi=5_000):
    """``submission.change_owner`` or ``change_developer`` (``role``)."""
    return Call(
        case,
        sender,
        "submission",
        f"change_{role}",
        {"contract": contract, f"new_{role}": holder},
        chi,
    )


def _owner_corpus() -> Corpus:
    return Corpus(
        "owner",
        (
            Block(
                (
                    Call(
                        "owner/info_before",
                        "alice",
                        "con_meta",
                        "info",
                        {"name": "con_owned_alice"},
                    ),
                    _change(
                        "owner/change_owner_too_little_chi",
                        "alice",
                        "con_owned_alice",
                        "owner",
                        "bob",
                        chi=70,
                    ),
                    _change("owner/change_owner", "alice", "con_owned_alice", "owner", "bob"),
                    Call("owner/old_owner_rejected", "alice", "con_owned_alice", "ping", {}),
                    Call("owner/new_owner_calls", "bob", "con_owned_alice", "ping", {}),
                    Call(
                        "owner/info_after", "alice", "con_meta", "info", {"name": "con_owned_alice"}
                    ),
                    _change("owner/change_owner_not_owner", "dev", "con_owned", "owner", "alice"),
                    _change("owner/change_owner_of_unowned", "dev", "con_unowned", "owner", "dev"),
                    _change("owner/change_owner_to_empty", "bob", "con_owned", "owner", ""),
                    _change("owner/change_owner_of_missing", "alice", "con_nope", "owner", "alice"),
                    _change("owner/change_developer", "dev", "con_probe", "developer", "dev2"),
                    Call(
                        "owner/rewards_to_new_developer",
                        "alice",
                        "con_probe",
                        "put",
                        {"key": "a", "value": 1},
                    ),
                    _change(
                        "owner/change_developer_not_developer",
                        "alice",
                        "con_probe",
                        "developer",
                        "alice",
                    ),
                    _change(
                        "owner/change_developer_to_empty", "dev2", "con_probe", "developer", ""
                    ),
                    Call(
                        "owner/set_owner_outside_submission",
                        "alice",
                        "con_meta",
                        "grab",
                        {"name": "con_unowned", "owner": "alice"},
                    ),
                    Call(
                        "owner/set_developer_outside_submission",
                        "alice",
                        "con_meta",
                        "rename",
                        {"name": "con_unowned", "developer": "alice"},
                    ),
                    Call(
                        "owner/info_of_missing", "alice", "con_meta", "info", {"name": "con_nope"}
                    ),
                )
            ),
        ),
    )


def _zk_corpus() -> Corpus:
    demo = _zk_demo()
    proof, inputs = demo["proof_hex"], demo["public_inputs"]
    wrong = ["0x" + "0" * 63 + "1"]
    return Corpus(
        "zk",
        (
            Block(
                (
                    Call("zk/available", "alice", "con_zk", "available", {}),
                    Call("zk/has_registered_key", "alice", "con_zk", "has_vk", {"vk_id": "demo"}),
                    Call("zk/has_unknown_key", "alice", "con_zk", "has_vk", {"vk_id": "nope"}),
                    Call("zk/info", "alice", "con_zk", "info", {"vk_id": "demo"}),
                    Call("zk/info_unknown", "alice", "con_zk", "info", {"vk_id": "nope"}),
                    Call(
                        "zk/verify",
                        "alice",
                        "con_zk",
                        "verify",
                        {"vk_hex": demo["vk_hex"], "proof_hex": proof, "public_inputs": inputs},
                        200_000,
                    ),
                    Call(
                        "zk/verify_wrong_input",
                        "alice",
                        "con_zk",
                        "verify",
                        {"vk_hex": demo["vk_hex"], "proof_hex": proof, "public_inputs": wrong},
                        200_000,
                    ),
                    Call(
                        "zk/verify_malformed_hex",
                        "alice",
                        "con_zk",
                        "verify",
                        {"vk_hex": "0xzz", "proof_hex": proof, "public_inputs": inputs},
                    ),
                    Call(
                        "zk/verify_registered",
                        "alice",
                        "con_zk",
                        "verify_registered",
                        {"vk_id": "demo", "proof_hex": proof, "public_inputs": inputs},
                        200_000,
                    ),
                    Call(
                        "zk/verify_registered_again",
                        "alice",
                        "con_zk",
                        "verify_registered",
                        {"vk_id": "demo", "proof_hex": proof, "public_inputs": inputs},
                        200_000,
                    ),
                    Call(
                        "zk/verify_unregistered",
                        "alice",
                        "con_zk",
                        "verify_registered",
                        {"vk_id": "nope", "proof_hex": proof, "public_inputs": inputs},
                        200_000,
                    ),
                    Call(
                        "zk/register",
                        "alice",
                        "zk_registry",
                        "register",
                        {"vk_id": "demo2", "vk_hex": demo["vk_hex"], "vk_hash": "0xdemo2"},
                    ),
                    Call(
                        "zk/verify_registered_earlier_in_block",
                        "alice",
                        "con_zk",
                        "verify_registered",
                        {"vk_id": "demo2", "proof_hex": proof, "public_inputs": inputs},
                        200_000,
                    ),
                    Call(
                        "zk/verify_low_chi",
                        "alice",
                        "con_zk",
                        "verify_registered",
                        {"vk_id": "demo", "proof_hex": proof, "public_inputs": inputs},
                        300,
                    ),
                )
            ),
            Block(
                (
                    Call("zk/call_with_zk_disabled", "alice", "con_zk", "available", {}),
                    Call("zk/importer_with_zk_disabled", "alice", "con_zk_importer", "get", {}),
                ),
                patch={"__runtime_features.zk": False},
            ),
        ),
    )


def _values_corpus() -> Corpus:
    return Corpus(
        "values",
        (
            Block(
                (
                    Call(
                        "values/kwargs_fixed", "alice", "con_values", "echo", {"x": _fixed("2.25")}
                    ),
                    Call(
                        "values/kwargs_big_int",
                        "alice",
                        "con_values",
                        "echo",
                        {"x": {"__big_int__": str(2**70)}},
                    ),
                    Call(
                        "values/kwargs_nested",
                        "alice",
                        "con_values",
                        "echo",
                        {"x": {"a": [1, _fixed("0.5"), {"b": None}], "c": True}},
                    ),
                    Call(
                        "values/kwargs_wrong_type",
                        "alice",
                        "con_values",
                        "typed",
                        {"value": "five"},
                    ),
                    Call(
                        "values/kwargs_unknown",
                        "alice",
                        "con_values",
                        "typed",
                        {"value": 1, "other": 2},
                    ),
                    Call(
                        "values/cyclic_return", "alice", "con_values", "cyclic", {"where": "return"}
                    ),
                    Call(
                        "values/cyclic_store", "alice", "con_values", "cyclic", {"where": "store"}
                    ),
                    Call(
                        "values/storage_key_at_limit",
                        "alice",
                        "con_values",
                        "long_key",
                        {"size": 480},
                    ),
                    Call(
                        "values/storage_key_too_long",
                        "alice",
                        "con_values",
                        "long_key",
                        {"size": 600},
                    ),
                    Call("entry/missing_contract", "alice", "con_nope", "get", {}),
                    Call("entry/not_exported", "alice", "con_probe", "missing", {}),
                    Call("entry/private_function", "alice", "con_probe", "hidden", {}),
                    Call(
                        "entry/private_called_internally", "alice", "con_probe", "private_call", {}
                    ),
                    Call("entry/owner_rejected", "alice", "con_owned", "ping", {}),
                    Call("entry/owner_allowed", "bob", "con_owned", "ping", {}),
                    Call("entry/source_only", "alice", "con_source_only", "ping", {}),
                    Call("entry/invalid_ir", "alice", "con_bad_ir", "ping", {}),
                    Call("entry/ir_not_text", "alice", "con_int_ir", "ping", {}),
                    Call("entry/import_cycle", "alice", "con_cycle_a", "get", {}),
                    Call("entry/missing_import", "alice", "con_missing_import", "get", {}),
                    Call(
                        "entry/negative_budget",
                        "alice",
                        "con_probe",
                        "put",
                        {"key": "n", "value": 1},
                        -5,
                    ),
                    Call(
                        "entry/zero_budget",
                        "alice",
                        "con_probe",
                        "put",
                        {"key": "z", "value": 1},
                        0,
                    ),
                )
            ),
        ),
    )


def _balances_corpus() -> Corpus:
    """Each case in a block of its own: some balances halt the node."""
    senders = ("alice", "bob", "carol", "dave", "erin", "fay", "gus", "hal", "ivy", "jon", "kim")
    calls = [
        Call(f"balance/{sender}_{label}", sender, "con_probe", function, kwargs, chi)
        for sender in senders
        for label, function, kwargs, chi in (
            ("success", "put", {"key": sender, "value": 1}, 3_000),
            ("failure", "fail_after_write", {"key": sender}, 3_000),
            ("base_budget", "put", {"key": sender, "value": 1}, 5),
        )
    ]
    calls += [
        Call("balance/exact_budget", "lea", "con_probe", "fail_after_write", {"key": "lea"}, 5),
        Call(
            "balance/transfer_to_str_balance",
            "alice",
            "currency",
            "transfer",
            {"amount": _fixed("1"), "to": "erin"},
        ),
        Call(
            "balance/transfer_to_list_balance",
            "alice",
            "currency",
            "transfer",
            {"amount": _fixed("1"), "to": "gus"},
        ),
        Call(
            "balance/pay_more_than_spendable",
            "mia",
            "con_probe",
            "pay",
            {"amount": _fixed("1.5")},
            2_000,
        ),
        Call(
            "balance/pay_within_spendable",
            "nia",
            "con_probe",
            "pay",
            {"amount": _fixed("0.6")},
            2_000,
        ),
    ]
    return Corpus("balances", tuple(Block((call,)) for call in calls))


def _rewards_corpus() -> Corpus:
    """Reward configurations the Rust port leaves to Python. Each one is set
    by a state patch (a broken split fails every paid transaction, the one
    that would repair it too) and restored by the next block's patch."""
    from xian_runtime_types.decimal import ContractingDecimal

    valid = {
        "rewards.S:value": [
            ContractingDecimal("0.5"),
            0,
            ContractingDecimal("0.3"),
            ContractingDecimal("0.2"),
        ],
        "foundation.owner": "foundation",
        "validators.active_validators": ["val1", "val2"],
        "validators.powers:val2": 3,
    }
    configurations = (
        ("split_text", "rewards.S:value", ["0.5", 0, "0.3", "0.2"]),
        ("split_short", "rewards.S:value", [1, 0, 0]),
        ("split_not_list", "rewards.S:value", "half"),
        ("split_empty", "rewards.S:value", []),
        ("split_dict", "rewards.S:value", {"a": 1}),
        ("split_member_none", "rewards.S:value", [1, 0, None, 0]),
        ("foundation_owner_missing", "foundation.owner", None),
        ("foundation_owner_int", "foundation.owner", 5),
        ("power_text", "validators.powers:val2", "3"),
        ("power_decimal", "validators.powers:val2", ContractingDecimal("2.5")),
        ("power_negative", "validators.powers:val2", -3),
        ("active_dict", "validators.active_validators", {"val1": 1}),
        ("active_int_member", "validators.active_validators", ["val1", 7]),
        ("active_empty", "validators.active_validators", []),
        ("active_text_balance", "validators.active_validators", ["val1", "val_bad"]),
    )
    blocks = [
        Block(
            (
                Call(
                    "rewards/split_changed_in_block",
                    "alice",
                    "rewards",
                    "set_split",
                    {"split": [_fixed("0.25"), 0, _fixed("0.25"), _fixed("0.5")]},
                ),
                Call(
                    "rewards/split_changed_in_block_then_success",
                    "alice",
                    "con_probe",
                    "put",
                    {"key": "r", "value": 1},
                ),
                Call(
                    "rewards/validators_changed_in_block", "alice", "validators", "rotate", {"n": 3}
                ),
                Call(
                    "rewards/validators_changed_in_block_then_failure",
                    "alice",
                    "con_probe",
                    "fail_after_write",
                    {"key": "r"},
                ),
            ),
            patch=valid,
        )
    ]
    for label, key, value in configurations:
        blocks.append(
            Block(
                (
                    Call(
                        f"rewards/{label}_success",
                        "alice",
                        "con_probe",
                        "put",
                        {"key": "r", "value": 1},
                    ),
                    Call(
                        f"rewards/{label}_failure",
                        "alice",
                        "con_probe",
                        "fail_after_write",
                        {"key": "r"},
                    ),
                ),
                patch={**valid, key: value},
            )
        )
    return Corpus("rewards", tuple(blocks))


def _policy_corpus(policy: str) -> Corpus:
    return Corpus(
        f"policy_{policy}",
        (
            Block(
                (
                    Call(
                        f"{policy}/success", "alice", "con_probe", "put", {"key": "p", "value": 1}
                    ),
                    Call(
                        f"{policy}/failure", "carol", "con_probe", "fail_after_write", {"key": "p"}
                    ),
                    _deploy(f"{policy}/deploy", "carol", "con_p1", sources.DEPLOYED),
                    Call(f"{policy}/random", "erin", "con_probe", "rnd_plain", {}),
                    Call(f"{policy}/rotate_validators", "alice", "validators", "rotate", {"n": 3}),
                    Call(f"{policy}/out_of_chi", "alice", "con_probe", "rnd_plain", {}, 30),
                )
            ),
        ),
        policy=policy,
    )


def corpora() -> tuple[Corpus, ...]:
    return (
        _deploy_corpus(),
        _random_corpus(),
        _owner_corpus(),
        _zk_corpus(),
        _values_corpus(),
        _balances_corpus(),
        _rewards_corpus(),
        _policy_corpus("free"),
        _policy_corpus("unmetered"),
    )


# --- running blocks ----------------------------------------------------------


def _tx(corpus: str, height: int, index: int, call: Call) -> dict:
    return {
        "payload": {
            "sender": call.sender,
            "contract": call.contract,
            "function": call.function,
            "kwargs": call.kwargs,
            "nonce": index,
            "chi_supplied": call.chi,
        },
        "metadata": {"signature": f"{corpus}-{height}-{index}"},
        "b_meta": block_meta(height),
    }


def block_meta(height: int) -> dict:
    return {
        "nanos": BASE_NANOS + height * 1_000_000_000,
        "height": height,
        "hash": f"block-{height}",
        "chain_id": CHAIN_ID,
    }


@contextmanager
def python_path() -> Iterator[None]:
    """Every transaction through ``process_tx``: the per-transaction path."""
    with mock.patch.object(TxProcessor, "_block_executor", lambda *_args: None):
        yield


@contextmanager
def _route_recorder(processor: TxProcessor) -> Iterator[list]:
    """Records ``(signature, reason)`` of each transaction ``process_tx`` ran:
    the reason ``process_block`` logged for a hand-back, else None."""
    routes: list[tuple[str, str | None]] = []
    reasons: list[str] = []
    process_tx = processor.process_tx

    def recorded(tx, *args, **kwargs):
        reason = reasons.pop() if reasons else None
        routes.append((tx["metadata"]["signature"], reason))
        return process_tx(tx, *args, **kwargs)

    def capture(message):
        reasons.append(message.record["extra"]["reason"])

    sink = logger.add(
        capture,
        level="DEBUG",
        filter=lambda record: record["extra"].get("stage") == "process_block",
    )
    try:
        with mock.patch.object(processor, "process_tx", recorded):
            yield routes
    finally:
        logger.remove(sink)


def _stored_digest(items: list[tuple[str, bytes | None]]) -> str:
    encoded = json.dumps(
        [[key, None if value is None else value.decode("utf-8")] for key, value in sorted(items)],
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Chain:
    driver: Any
    processor: TxProcessor
    rewards: Any
    roots: Any
    policy: TxFeePolicy


def run_corpus(home: Path, corpus: Corpus, *, python: bool) -> tuple[dict, dict[str, str]]:
    """The corpus's receipts, block writes and app hashes on one path, and
    the route of every transaction: ``RUST``, the hand-back reason, or
    ``PER_TX_PATH``."""
    from xian.state_root import StateRootCache

    client, processor, rewards = open_chain(home)
    driver = client.raw_driver
    policy = POLICIES[corpus.policy]()
    chain = _Chain(driver, processor, rewards, StateRootCache.from_driver(driver), policy)
    blocks, routes = [], {}
    try:
        with ExitStack() as stack:
            if python:
                stack.enter_context(python_path())
            recorded = stack.enter_context(_route_recorder(processor))
            for height, block in enumerate(corpus.blocks, start=1):
                txs = [_tx(corpus.name, height, i, call) for i, call in enumerate(block.calls)]
                blocks.append(_run_block(chain, height, block, txs))
                routes.update(_routes(txs, block.calls, dict(recorded)))
    finally:
        driver.close()
    return {"policy": corpus.policy, "blocks": blocks}, routes


def _run_block(chain: _Chain, height: int, block: Block, txs: list[dict]) -> dict:
    """One block, committed. A block whose processing or receipt encoding
    raises halts the node (``FinalizeBlock`` raises) and never commits."""
    for key, value in block.patch.items():
        chain.driver.set(key, value)
    try:
        responses = chain.processor.process_block(
            txs, [transaction_bytes(tx) for tx in txs], fee_policy=chain.policy, rewards_handler=chain.rewards
        )
        receipts = [
            _receipt(call, response) for call, response in zip(block.calls, responses, strict=True)
        ]
    except Exception as exc:  # noqa: BLE001 - recorded: the node halts here
        chain.driver.rollback()
        return {
            "height": height,
            "halt": type(exc).__name__,
            "cases": [call.case for call in block.calls],
        }
    stored = chain.driver.pending_stored_items()
    outcome = {
        "height": height,
        "txs": receipts,
        "write_count": len(stored),
        "writes_sha256": _stored_digest(stored),
        "app_hash": chain.roots.prepare(stored).hex(),
    }
    chain.roots.commit()
    chain.driver.commit()
    return outcome


def _receipt(call: Call, response: dict) -> dict:
    """The receipt as ``FinalizeBlock`` puts it in ``ExecTxResult``."""
    from xian.utils.encoding import encode_abci_json

    tx_result = response["tx_result"]
    return {
        "case": call.case,
        "code": tx_result["status"],
        "data": encode_abci_json(tx_result).decode("utf-8"),
    }


def _routes(txs: list[dict], calls: tuple[Call, ...], ran_in_python: dict) -> dict[str, str]:
    routes = {}
    for tx, call in zip(txs, calls, strict=True):
        signature = tx["metadata"]["signature"]
        if signature not in ran_in_python:
            routes[call.case] = RUST
        else:
            routes[call.case] = ran_in_python[signature] or PER_TX_PATH
    return routes


def handed_back(routes: dict[str, str]) -> dict[str, str]:
    """The cases that ran on the Python path, with the hand-back reason."""
    return {case: route for case, route in routes.items() if route != RUST}


# --- simulation and contract views --------------------------------------------


@dataclass(frozen=True)
class Simulation:
    case: str
    payload: dict
    max_chi: int = 100_000
    charge_fees: bool = True


def simulations() -> tuple[Simulation, ...]:
    def sim(case, sender, contract, function, kwargs, **options):
        payload = {"sender": sender, "contract": contract, "function": function, "kwargs": kwargs}
        return Simulation(case, payload, **options)

    return (
        sim("simulate/success", "alice", "con_probe", "put", {"key": "s", "value": 2}),
        sim("simulate/failure", "alice", "con_probe", "fail_after_write", {"key": "s"}),
        sim("simulate/unfunded_success", "carol", "con_probe", "put", {"key": "s", "value": 2}),
        sim("simulate/unfunded_failure", "carol", "con_probe", "fail_after_write", {"key": "s"}),
        sim("simulate/str_balance", "erin", "con_probe", "put", {"key": "s", "value": 2}),
        sim("simulate/missing_contract", "alice", "con_nope", "get", {}),
        sim("simulate/not_exported", "alice", "con_probe", "hidden", {}),
        sim("simulate/owner_rejected", "alice", "con_owned", "ping", {}),
        sim("simulate/out_of_chi", "alice", "con_probe", "rnd_plain", {}, max_chi=40),
        sim(
            "simulate/transfer",
            "alice",
            "currency",
            "transfer",
            {"amount": _fixed("1.5"), "to": "bob"},
        ),
        sim(
            "simulate/deploy",
            "alice",
            "submission",
            "submit_contract",
            {"name": "con_sim", "code": sources.DEPLOYED},
        ),
        sim(
            "simulate/deploy_bad_source",
            "alice",
            "submission",
            "submit_contract",
            {"name": "con_sim", "code": sources.DEPLOYED_SYNTAX_ERROR},
        ),
        sim(
            "simulate/factory",
            "alice",
            "con_factory",
            "make_and_call",
            {"name": "con_sim2", "code": sources.DEPLOYED},
        ),
        sim("simulate/random", "alice", "con_probe", "rnd_plain", {}),
        sim("simulate/unseeded_random", "alice", "con_probe", "rnd_unseeded", {}),
        sim(
            "simulate/change_owner",
            "alice",
            "submission",
            "change_owner",
            {"contract": "con_owned_alice", "new_owner": "bob"},
        ),
        sim("simulate/zk_registry", "alice", "con_zk", "has_vk", {"vk_id": "demo"}),
        sim("simulate/bad_kwargs", "alice", "con_values", "echo", {"x": _fixed("abc")}),
        sim("simulate/float_kwargs", "alice", "con_values", "echo", {"x": 1.5}),
        sim(
            "simulate/free_success",
            "carol",
            "con_probe",
            "put",
            {"key": "s", "value": 2},
            charge_fees=False,
        ),
        sim(
            "simulate/free_failure",
            "carol",
            "con_probe",
            "fail_after_write",
            {"key": "s"},
            charge_fees=False,
        ),
    )


SIMULATION_HEIGHT = 7


def _encoded(result: Any) -> dict:
    """The query response value; a result it cannot encode fails the query."""
    from xian.methods.query import _encode_query_value

    try:
        value, kind = _encode_query_value(result)
    except Exception as exc:  # noqa: BLE001 - recorded: the query fails
        return {"error": type(exc).__name__}
    return {"type": kind, "value": None if value is None else value.decode("utf-8")}


def _simulate(client, simulation: Simulation, raw_hex: str, *, python: bool) -> Any:
    """The ``simulate_tx`` query result: decoded as the query decodes it,
    then run in-process (Python path) or in the simulation worker."""
    from xian.simulator import QuerySimulationService, TransactionSimulator

    meta = block_meta(SIMULATION_HEIGHT)
    service = QuerySimulationService(
        storage_home=client.raw_driver.storage_home,
        get_block_meta=lambda: meta,
        get_state_snapshot=client.raw_driver.snapshot_state,
        chain_id=CHAIN_ID,
        max_chi=simulation.max_chi,
        charge_fees=simulation.charge_fees,
        timeout_ms=60_000,
    )
    prepared = service.prepare_encoded_transaction(raw_hex)
    if isinstance(prepared, dict):
        return prepared
    if not python:
        return asyncio.run(prepared.run())
    simulator = TransactionSimulator(
        client=client,
        get_block_meta=lambda: meta,
        chain_id=CHAIN_ID,
        charge_fees=simulation.charge_fees,
    )
    return simulator.simulate(prepared.payload, max_chi=simulation.max_chi)


def run_simulations(home: Path, *, python: bool) -> dict:
    """Each simulation's query response on one path."""
    client, _processor, _rewards = open_chain(home)
    results = {}
    try:
        for simulation in simulations():
            raw = json.dumps(simulation.payload, separators=(",", ":")).encode("utf-8")
            result = _simulate(client, simulation, raw.hex(), python=python)
            results[simulation.case] = _encoded(result)
    finally:
        client.raw_driver.close()
    return results


# (case, sender, contract, function, kwargs)
CONTRACT_VIEWS = (
    ("view/dict", "alice", "con_probe", "put", {"key": "v", "value": 3}),
    ("view/decimal", "bob", "con_probe", "pay", {"amount": _fixed("0.5")}),
    ("view/metadata", "alice", "con_meta", "info", {"name": "con_probe"}),
    ("view/failure", "alice", "con_probe", "fail_after_write", {"key": "v"}),
    ("view/missing_contract", "alice", "con_nope", "get", {}),
    ("view/not_exported", "alice", "con_probe", "hidden", {}),
    ("view/owner_rejected", "alice", "con_owned", "ping", {}),
    ("view/owner_allowed", "bob", "con_owned", "ping", {}),
    ("view/random", "alice", "con_probe", "rnd_plain", {}),
    ("view/zk", "alice", "con_zk", "has_vk", {"vk_id": "demo"}),
    ("view/import_cycle", "alice", "con_cycle_a", "get", {}),
    (
        "view/deploy",
        "alice",
        "submission",
        "submit_contract",
        {"name": "con_view", "code": sources.DEPLOYED},
    ),
)


def _python_contract_view(ctx, contract: str, function: str, kwargs: dict) -> Any:
    """A contract view on the per-transaction functions, without metering."""
    from xian.execution_engine import execute_vm_contract, prepare_vm_contract
    from xian.methods.query import _make_vm_query_environment

    driver = ctx.raw_driver
    if not driver.has_contract(contract):
        return None
    state = driver.snapshot_state()
    try:
        prepare_vm_contract(ctx.app.execution_runtime, driver, contract)
        output = execute_vm_contract(
            ctx.app.execution_runtime,
            driver,
            sender=ctx.key,
            contract_name=contract,
            function_name=function,
            kwargs=dict(kwargs),
            environment=_make_vm_query_environment(ctx),
            meter=False,
        )
    finally:
        driver.restore_state(state)
    return output.result if output.status_code == 0 else None


def run_contract_views(home: Path, *, python: bool) -> dict:
    """Each contract view's query response on one path; a view that raises
    fails the query."""
    from xian.execution_engine import VmRuntime
    from xian.methods.query import QueryContext, _call_contract_view

    client, _processor, _rewards = open_chain(home)
    app = SimpleNamespace(
        client=client,
        execution_runtime=VmRuntime(),
        current_block_meta=block_meta(SIMULATION_HEIGHT),
        chain_id=CHAIN_ID,
    )
    view = _python_contract_view if python else _call_contract_view
    results = {}
    try:
        for case, sender, contract, function, kwargs in CONTRACT_VIEWS:
            ctx = QueryContext(app=app, path_parts=[], params={}, route="view", key=sender)
            try:
                result = view(ctx, contract, function, kwargs=kwargs)
            except Exception as exc:  # noqa: BLE001 - recorded: the query fails
                results[case] = {"error": type(exc).__name__}
                continue
            results[case] = _encoded(result)
    finally:
        client.raw_driver.close()
    return results


# --- system transactions through FinalizeBlock ---------------------------------

NODE_1 = "11" * 32
NODE_2 = "22" * 32
NODE_3 = "33" * 32
NODE_4 = "44" * 32


@dataclass(frozen=True)
class SystemBlock:
    height: int
    # (misbehavior type, validator key, evidence height)
    evidence: tuple[tuple[int, str, int], ...] = ()
    patch: dict[str, Any] = field(default_factory=dict)


def system_blocks() -> tuple[SystemBlock, ...]:
    rebalance_ready = {
        "validators.candidates": [NODE_4],
        f"validators.statuses:{NODE_4}": "approved",
        f"validators.pending_registrations:{NODE_4}": False,
        f"validators.pending_leave:{NODE_4}": False,
        f"validators.self_bond:{NODE_4}": 500,
        f"validators.total_delegated:{NODE_4}": 0,
        f"validators.delegator_lists:{NODE_4}": [],
        f"validators.requested_power:{NODE_4}": 21,
        f"validators.powers:{NODE_4}": 0,
        f"validators.reward_keys:{NODE_4}": NODE_4,
        f"validators.eligible_at_epoch:{NODE_4}": 1,
        "validators.config:selection_mode": "auto_top_n",
        "validators.config:max_validators": 3,
        "validators.config:power_mode": "requested",
        "validators.config:rebalance_interval": 5,
        "validators.config:min_self_bond": 0,
        "validators.config:min_total_bond": 0,
        "validators.last_rebalance_epoch": 0,
    }
    return (
        SystemBlock(
            1, patch={f"validators.self_bond:{NODE_1}": 200, "currency.balances:validators": 400}
        ),
        SystemBlock(2, evidence=((1, NODE_1, 2),)),
        SystemBlock(3, evidence=((2, NODE_2, 3), (1, "ff" * 32, 3), (9, NODE_3, 3))),
        SystemBlock(4, evidence=((1, NODE_1, 2),), patch=rebalance_ready),
        SystemBlock(5),
        SystemBlock(6),
        SystemBlock(10, patch={"validators.config:power_mode": "bogus"}),
        SystemBlock(11, evidence=((1, NODE_3, 11),)),
        # A penalty that fails (a corrupted bond): a failed system receipt.
        SystemBlock(
            12, evidence=((1, NODE_2, 12),), patch={f"validators.self_bond:{NODE_2}": "bogus"}
        ),
    )


@contextmanager
def _node_home() -> Iterator[type]:
    """A node home copied from the integration fixture, with the paths the
    pytest fixtures patch (``tests/conftest.py``)."""
    from contracting import constants as contracting_constants

    from xian.constants import Constants

    with tempfile.TemporaryDirectory(prefix="engine-parity-node-") as tmp:
        home = Path(tmp) / ".cometbft"
        shutil.copytree(NODE_FIXTURE, home)

        class NodeConstants(Constants):
            COMETBFT_HOME = home
            COMETBFT_CONFIG = home / "config/config.toml"
            XIAN_CONFIG = home / "config/xian.toml"
            COMETBFT_GENESIS = home / "config/genesis.json"
            STORAGE_HOME = home / "xian"

        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict("os.environ", {"HOME": tmp}))
            for name, value in vars(NodeConstants).items():
                if name.isupper():
                    stack.enter_context(mock.patch.object(Constants, name, value))
            stack.enter_context(
                mock.patch.object(contracting_constants, "STORAGE_HOME", NodeConstants.STORAGE_HOME)
            )
            yield NodeConstants


def _misbehavior(kind: int, validator_key: str, height: int):
    from google.protobuf.timestamp_pb2 import Timestamp

    from cometbft.abci.v1beta1.types_pb2 import Validator
    from cometbft.abci.v1beta2.types_pb2 import Misbehavior

    evidence_time = Timestamp()
    evidence_time.seconds = height
    return Misbehavior(
        type=kind,
        validator=Validator(
            address=hashlib.sha256(bytes.fromhex(validator_key)).digest()[:20], power=10
        ),
        height=height,
        time=evidence_time,
        total_voting_power=30,
    )


def _deploy_system_contracts(client) -> None:
    """The genesis contracts the validator system transactions use, deployed
    at the fixed submission time (the client stamps the wall clock)."""
    from xian_runtime_types.time import Datetime

    from xian.config_paths import resolve_contracts_dir

    contracts_dir = resolve_contracts_dir()
    deployments = (
        ("currency", {"vk": "xian-deployer"}),
        ("dao", None),
        ("rewards", None),
        ("chi_cost", {"initial_rate": 20}),
        (
            "validators",
            {"genesis_nodes": [NODE_1, NODE_2, NODE_3], "genesis_registration_fee": 1000},
        ),
    )
    _fix_submission_time(client.raw_driver)
    with mock.patch.dict(client.environment, {"now": Datetime(*SUBMITTED)}):
        for contract, args in deployments:
            client.submit(
                (contracts_dir / f"{contract}.s.py").read_text(encoding="utf-8"),
                name=contract,
                constructor_args=args,
            )


async def _finalize(handler, block: SystemBlock):
    """``FinalizeBlock`` then ``Commit`` of an empty block with the block's
    evidence; returns the ``ResponseFinalizeBlock``."""
    from io import BytesIO

    from google.protobuf.timestamp_pb2 import Timestamp

    from abci.utils import read_messages
    from cometbft.abci.v1beta1.types_pb2 import RequestCommit
    from cometbft.abci.v1beta3.types_pb2 import Request, RequestFinalizeBlock, Response

    timestamp = Timestamp()
    timestamp.seconds = block.height
    request = Request(
        finalize_block=RequestFinalizeBlock(
            height=block.height,
            time=timestamp,
            txs=[],
            misbehavior=[_misbehavior(*entry) for entry in block.evidence],
            hash=f"block-{block.height}".encode("utf-8"),
        )
    )
    raw = await handler.process("finalize_block", request)
    response = next(read_messages(BytesIO(raw), Response)).finalize_block
    await handler.process("commit", Request(commit=RequestCommit()))
    return response


# The system transaction cases, by the prefix of their signature.
SYSTEM_CASES = {
    "validator-epoch-rebalance": "system/epoch_rebalance",
    "evidence-penalty": "system/evidence_penalty",
}


def _system_routes(ran_in_python: list[tuple[str, str | None]]) -> dict[str, str]:
    """The system transaction cases that ran on the Python path."""
    return {
        SYSTEM_CASES[signature.split(":", 1)[0]]: reason or PER_TX_PATH
        for signature, reason in ran_in_python
    }


async def _run_system_blocks(constants, *, python: bool) -> tuple[dict, dict[str, str]]:
    from abci.server import ProtocolHandler
    from xian.utils.encoding import encode_abci_json
    from xian.xian_abci import Xian

    app = await Xian.create(constants=constants)
    try:
        handler = ProtocolHandler(app)
        _deploy_system_contracts(app.client)
        receipts: list[str] = []
        process_system_tx = app.tx_processor.process_system_tx

        def recorded(tx):
            response = process_system_tx(tx)
            receipts.append(encode_abci_json(response["tx_result"]).decode("utf-8"))
            return response

        blocks = []
        with ExitStack() as stack:
            if python:
                stack.enter_context(python_path())
            ran_in_python = stack.enter_context(_route_recorder(app.tx_processor))
            stack.enter_context(mock.patch.object(app.tx_processor, "process_system_tx", recorded))
            for block in system_blocks():
                for key, value in block.patch.items():
                    app.client.raw_driver.set(key, value)
                receipts.clear()
                response = await _finalize(handler, block)
                blocks.append(
                    {
                        "height": block.height,
                        "system_receipts": list(receipts),
                        "response": response.SerializeToString(deterministic=True).hex(),
                        "app_hash": response.app_hash.hex(),
                    }
                )
        return {"blocks": blocks}, _system_routes(ran_in_python)
    finally:
        app.client.close()
        await app.close()


def run_system_blocks(*, python: bool) -> tuple[dict, dict[str, str]]:
    """The system blocks' outputs on one path, and the system transaction
    cases that ran on the Python path."""
    with _node_home() as constants:
        return asyncio.run(_run_system_blocks(constants, python=python))


# --- the frozen fixtures --------------------------------------------------------


def fixture_path(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def load_fixture(name: str) -> dict:
    return json.loads(fixture_path(name).read_text(encoding="utf-8"))


def _write_fixture(name: str, outputs: dict) -> None:
    text = json.dumps(outputs, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    fixture_path(name).write_text(text, encoding="utf-8")


def python_path_outputs(tmp: Path) -> dict[str, dict]:
    """Every golden fixture's content, computed on the Python path."""
    outputs = {}
    for corpus in corpora():
        outputs[f"blocks_{corpus.name}"], _routes = run_corpus(
            tmp / corpus.name, corpus, python=True
        )
    outputs["simulations"] = run_simulations(tmp / "simulations", python=True)
    outputs["contract_views"] = run_contract_views(tmp / "views", python=True)
    outputs["system_blocks"], _routes = run_system_blocks(python=True)
    return outputs


# What runs outside ``process_block`` today, and where.
OUTSIDE_BLOCK_EXECUTOR = {
    "simulations": "simulator worker: execute_vm_transaction",
    "contract_views": "execute_vm_contract",
}


def handback_inventory(tmp: Path) -> dict[str, dict[str, str]]:
    """The cases the production path runs on the Python path, by corpus:
    measured for blocks and system transactions, by entry point for the rest."""
    inventory = {
        corpus.name: handed_back(run_corpus(tmp / corpus.name, corpus, python=False)[1])
        for corpus in corpora()
    }
    inventory["system_blocks"] = run_system_blocks(python=False)[1]
    inventory["simulations"] = {
        simulation.case: OUTSIDE_BLOCK_EXECUTOR["simulations"] for simulation in simulations()
    }
    inventory["contract_views"] = {
        case: OUTSIDE_BLOCK_EXECUTOR["contract_views"] for case, *_ in CONTRACT_VIEWS
    }
    return inventory


def _write(writer: Callable[[Path], dict[str, dict]]) -> None:
    with tempfile.TemporaryDirectory(prefix="engine-parity-") as tmp:
        for name, outputs in writer(Path(tmp)).items():
            _write_fixture(name, outputs)


if __name__ == "__main__":
    commands = {
        "--write": python_path_outputs,
        "--write-inventory": lambda tmp: {"handback_inventory": handback_inventory(tmp)},
    }
    if sys.argv[1:] not in [[command] for command in commands]:
        raise SystemExit(__doc__)
    _write(commands[sys.argv[1]])
