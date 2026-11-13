"""Blocks executed by the Rust block executor against the per-transaction path.

``TxProcessor.process_block`` runs a block's transactions in Rust and hands
the ones that need Python (contract submission, random, owner rejections)
to ``process_tx`` in their place. Receipts, pending state and responses must
be byte-identical to processing every transaction with ``process_tx``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from contracting.local import ContractingClient
from loguru import logger
from xian_runtime_types.encoding import decode_wire_numbers, encode
from xian_runtime_types.time import Timedelta

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler
from xian.utils.encoding import encode_abci_json

pytest.importorskip("xian_vm_core")

ROOT = Path(__file__).resolve().parents[3]
PROBE = """import currency
items = Hash(default_value=0)
counter = Variable(default_value=0)
Ev = LogEvent("Ev", {"key": indexed(str), "value": int})

@export
def put(key: str, value: int):
    items[key] = value
    counter.set(counter.get() + 1)
    Ev({"key": key, "value": value})
    return {"counter": counter.get(), "key": key}

@export
def fail_after_write(key: str):
    items[key] = 1
    assert False, "forced failure"

@export
def pay(amount: decimal):
    currency.transfer_from(amount=amount, to="recipient", main_account=ctx.caller)
    return currency.balance_of(address=ctx.caller)

@export
def probe(name: str):
    return [importlib.exists(name), importlib.has_export(name, "transfer")]

@export
def lucky():
    random.seed()
    return random.getrandbits(16)

@export
def burn(n: int):
    x = 7
    for i in range(n):
        x = (x * 31 + i) % 1000003
    return x
"""
OWNED = """@export
def ping():
    return 1
"""
# Stands in for the validators contract: transactions in a block change the
# active set the fee rewards (of failures too) are paid to.
VALIDATORS = """active_validators = Variable()
powers = Hash()

@export
def rotate(n: int):
    active_validators.set(["val1", "val2", "val3"][:n])
    powers["val3"] = n
"""
DEPLOYED = """v = Variable()

@construct
def seed():
    v.set(7)

@export
def get():
    return v.get()
"""
SCAN = """import con_probe
items = Hash(default_value=0)
copy = Hash()
probe_items = ForeignHash(foreign_contract="con_probe", foreign_name="items")
balances = ForeignHash(foreign_contract="currency", foreign_name="balances")

@export
def fill(group: str, n: int):
    for i in range(n):
        items[group, str(i)] = i
    return len(items.all(group))

@export
def fill_text(group: str, n: int, size: int):
    for i in range(n):
        items[group, str(i)] = "z" * size

@export
def put(group: str, key: str, value: int):
    items[group, key] = value

@export
def drop(group: str, key: str):
    items[group, key] = None

@export
def scan(group: str):
    return items.all(group)

@export
def scan_everything():
    return items.all()

@export
def write_then_scan(group: str):
    items[group, "w"] = 99
    items[group, "0"] = None
    return items.all(group)

@export
def clear(group: str):
    items.clear(group)
    return items.all()

@export
def clone():
    copy.clone_from(items)
    return copy.all()

@export
def foreign():
    return [probe_items.all(), balances.all()]

@export
def nested_then_scan(key: str):
    con_probe.put(key=key, value=3)
    return probe_items.all()

@export
def clear_then_fail(group: str):
    items.clear(group)
    assert False, "rolled back"

@export
def scan_then_random(group: str):
    values = items.all(group)
    random.seed()
    return [values, random.getrandbits(8)]

@export
def info(name: str):
    return [importlib.contract_info(name), importlib.contract_info(contract=name)]

@export
def module_info(name: str):
    module = importlib.import_module(name)
    return [
        importlib.contract_info(module),
        importlib.code_hash(module, kind="source"),
        importlib.owner_of(module),
        importlib.exists(module),
        importlib.has_export(module, "put"),
    ]

@export
def module_kind(name: str):
    return importlib.code_hash("con_scan", importlib.import_module(name))

@export
def hashes(name: str, kind: str):
    return [importlib.code_hash(name), importlib.code_hash(name, kind), importlib.code_hash(name, kind=kind)]
"""
TIMES = """span = Variable()
spans = Hash()
raw = Variable()
Seen = LogEvent("Seen", {"name": indexed(str), "spans": set, "raw": bytearray})

@export
def keep(days: int, seconds: int):
    span.set(datetime.timedelta(days=days, seconds=seconds))
    spans[days] = {"pair": [span.get(), datetime.timedelta(days=-days)]}
    return span.get()

@export
def read(days: int):
    return [span.get(), spans[days]]

@export
def keep_arg(d: datetime.timedelta):
    spans["arg"] = d
    return spans["arg"]

@export
def keep_bytes():
    raw.set(bytearray(b"ab\\xff"))
    return raw.get()

@export
def announce(seconds: int):
    Seen({"name": "x", "spans": set([datetime.timedelta(seconds=seconds)]), "raw": bytearray(b"q")})
"""
ACCOUNTS = {"alice": 100_000, "bob": "250.5", "carol": None, "dave": "0.0003"}


def _setup(home, contracts=()):
    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=home)
    driver = client.raw_driver
    from xian_runtime_types.decimal import ContractingDecimal
    from xian_runtime_types.time import Datetime

    # Fixed submission times: contract.info returns them.
    submitted = Datetime(2026, 1, 2, 3, 4, 5)
    driver.set_contract_from_source(
        "currency",
        (ROOT / "xian-configs/contracts/currency.s.py").read_text(),
        timestamp=submitted,
    )
    driver.set_contract_from_source("con_probe", PROBE, developer="dev", timestamp=submitted)
    driver.set_contract_from_source("con_owned", OWNED, owner="bob", timestamp=submitted)
    driver.set_contract_from_source(
        "con_scan",
        SCAN,
        developer="dev",
        deployer="deployer",
        initiator="initiator",
        timestamp=submitted,
    )
    driver.set_contract_from_source("con_times", TIMES, timestamp=submitted)
    driver.set_contract_from_source("validators", VALIDATORS, timestamp=submitted)
    for name, source, developer in contracts:
        driver.set_contract_from_source(
            name, source, developer=developer, timestamp=submitted, overwrite=True
        )

    for account, balance in ACCOUNTS.items():
        if balance is not None:
            value = ContractingDecimal(balance) if isinstance(balance, str) else balance
            driver.set(f"currency.balances:{account}", value)
        driver.set(f"currency.approvals:{account}:con_probe", 50)
    driver.set("chi_cost.S:value", 20)
    driver.set("foundation.owner", "foundation")
    driver.set("validators.active_validators", ["val1", "val2"])
    driver.set("validators.powers:val2", 3)
    driver.set(
        "rewards.S:value",
        [ContractingDecimal("0.5"), 0, ContractingDecimal("0.3"), ContractingDecimal("0.2")],
    )
    driver.commit()
    return client, TxProcessor(client=client), RewardsHandler(client)


OPERATIONS = [
    (
        "con_probe",
        "put",
        lambda rng: {"key": rng.choice("abc"), "value": rng.randrange(-5, 50)},
        3000,
    ),
    ("con_probe", "fail_after_write", lambda rng: {"key": "a"}, 3000),
    (
        "con_probe",
        "pay",
        lambda rng: {"amount": {"__fixed__": rng.choice(["0.5", "1", "1000"])}},
        5000,
    ),
    ("con_probe", "probe", lambda rng: {"name": rng.choice(["currency", "con_nope"])}, 3000),
    ("con_probe", "burn", lambda rng: {"n": rng.choice([10, 5000])}, 40),
    ("con_probe", "lucky", lambda rng: {}, 3000),
    ("con_owned", "ping", lambda rng: {}, 1000),
    ("currency", "transfer", lambda rng: {"amount": {"__fixed__": "0.1"}, "to": "erin"}, 1000),
    ("con_probe", "missing", lambda rng: {}, 1000),
    ("validators", "rotate", lambda rng: {"n": rng.randrange(0, 4)}, 1000),
]
# Need Python once the fee is reserved: random, an owner rejection (bob owns
# con_owned), a function that is not exported, contract submission.
PYTHON_ONLY = {("con_probe", "lucky"), ("con_owned", "ping"), ("con_probe", "missing")}


def _block(seed, size, height):
    rng = random.Random(seed)
    txs = []
    for index in range(size):
        contract, function, kwargs, budget = rng.choice(OPERATIONS)
        sender = rng.choice(list(ACCOUNTS))
        if index == size // 2:
            contract, function, budget = "submission", "submit_contract", 100_000
            kwargs = lambda rng, h=height: {"name": f"con_new_{h}", "code": DEPLOYED}  # noqa: E731
        txs.append(_tx(seed, index, height, sender, contract, function, kwargs(rng), budget))
    return txs


def _tx(seed, index, height, sender, contract, function, kwargs, budget):
    return {
        "payload": {
            "sender": sender,
            "contract": contract,
            "function": function,
            "kwargs": kwargs,
            "nonce": index,
            "chi_supplied": budget,
        },
        "metadata": {"signature": f"sig-{seed}-{index}"},
        "b_meta": {
            "nanos": 1_700_000_000_000_000_000 + height,
            "height": height,
            "hash": f"block-{height}",
            "chain_id": "xian-local",
        },
    }


def _run(home, blocks, *, native, monkeypatch=None, policy=None, contracts=()):
    client, processor, rewards = _setup(home, contracts)
    policy = policy or TxFeePolicy.paid_metered()
    python_runs = []
    if monkeypatch is not None:
        process_tx = processor.process_tx

        def counted(tx, *args, **kwargs):
            payload = tx["payload"]
            python_runs.append((payload["sender"], payload["contract"], payload["function"]))
            return process_tx(tx, *args, **kwargs)

        monkeypatch.setattr(processor, "process_tx", counted)
    receipts = []
    for txs in blocks:
        if native:
            responses = processor.process_block(txs, fee_policy=policy, rewards_handler=rewards)
        else:
            responses = [processor.process_tx(tx, rewards, fee_policy=policy) for tx in txs]
        for response in responses:
            receipts.append(
                (
                    encode_abci_json(response["tx_result"]),
                    response["chi_rewards_amount"],
                    response["chi_rewards_contract"],
                )
            )
        receipts.append(encode(client.raw_driver.pending_items()))
        client.raw_driver.commit()
    client.raw_driver.close()
    return receipts, python_runs


def test_rust_block_execution_matches_the_per_transaction_path(tmp_path, monkeypatch):
    blocks = [_block(seed, 40, height) for height, seed in enumerate((11, 12, 13), start=1)]
    expected, _ = _run(tmp_path / "python", blocks, native=False)
    actual, python_runs = _run(tmp_path / "rust", blocks, native=True, monkeypatch=monkeypatch)
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert left == right, index
    # Failures pay their fee out too, also to a validator set changed earlier
    # in the block.
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    paid_failures = [r for r in receipts if r["status"] == 1 and r["rewards"]]
    assert len(paid_failures) > 10
    assert any("val3" in r["rewards"]["validator_reward"] for r in paid_failures)

    # Only transactions that need Python ran through process_tx, and all of
    # alice's (she can always pay the fee).
    def needs_python(sender, contract, function):
        if contract == "submission" or (contract, function) == ("con_owned", "ping"):
            return contract == "submission" or sender != "bob"
        return (contract, function) in PYTHON_ONLY

    assert all(needs_python(*run) for run in python_runs)
    alice = [
        ("alice", tx["payload"]["contract"], tx["payload"]["function"])
        for txs in blocks
        for tx in txs
        if tx["payload"]["sender"] == "alice"
        and needs_python("alice", tx["payload"]["contract"], tx["payload"]["function"])
    ]
    assert [run for run in python_runs if run[0] == "alice"] == alice
    assert len(python_runs) < sum(len(txs) for txs in blocks) // 2


def test_unmetered_system_execution_matches_the_per_transaction_path(tmp_path, monkeypatch):
    # System transactions (epoch rebalance, evidence penalties) run in Rust
    # unmetered: no chi budget (burn's 40 chi do not run out), no failure
    # charge, no fee, no rewards.
    policy = TxFeePolicy.system_unmetered()
    blocks = [_block(seed, 40, height) for height, seed in enumerate((31, 32), start=1)]
    expected, _ = _run(tmp_path / "python", blocks, native=False, policy=policy)
    actual, python_runs = _run(
        tmp_path / "rust", blocks, native=True, monkeypatch=monkeypatch, policy=policy
    )
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert left == right, index
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    assert {receipt["status"] for receipt in receipts} == {0, 1}
    assert not any(receipt["rewards"] for receipt in receipts)

    # Only the hand-backs ran through process_tx: without a fee to reserve,
    # every sender reaches them.
    def needs_python(sender, contract, function):
        if (contract, function) == ("con_owned", "ping"):
            return sender != "bob"
        return contract == "submission" or (contract, function) in PYTHON_ONLY

    calls = [
        (tx["payload"]["sender"], tx["payload"]["contract"], tx["payload"]["function"])
        for txs in blocks
        for tx in txs
    ]
    assert python_runs == [call for call in calls if needs_python(*call)]
    assert len(python_runs) < len(calls) // 2


# A submission contract that changes contract metadata (only submission may).
METADATA = """@export
def change(n: int, developer: str):
    Contract.set_owner("con_scan", "o" * n)
    Contract.set_developer("con_probe", developer)
    return n
"""
# Host charges count for the transaction's entry contract: here the relay, so
# the developer reward splits differently from the costs the VM measured.
RELAY = """import submission

@export
def relay(n: int, developer: str):
    return submission.change(n=n, developer=developer)
"""
POLICIES = {
    "paid": TxFeePolicy.paid_metered,
    "free": TxFeePolicy.free_metered,
    "unmetered": TxFeePolicy.system_unmetered,
}


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_contract_metadata_changes_run_in_rust_like_the_python_path(tmp_path, monkeypatch, policy):
    # Each change stages its write and, metered, charges (encoded key +
    # value) * 25 raw to the shared budget: around 50 chi for a 2000-byte
    # owner, so the smaller budgets run out of chi in the host, the larger
    # ones succeed and pay the developer reward of the entry contract.
    calls = [
        ("alice", "submission", "change", {"n": 2000, "developer": f"dev{budget}"}, budget)
        for budget in range(20, 90, 3)
    ]
    calls += [
        ("alice", "con_relay", "relay", {"n": 300, "developer": "relayed"}, 3000),
        ("alice", "con_probe", "put", {"key": "a", "value": 1}, 3000),
    ]
    blocks = [[_tx(300, index, 1, *call) for index, call in enumerate(calls)]]
    contracts = [("submission", METADATA, "submission_dev"), ("con_relay", RELAY, "relay_dev")]
    options = {"policy": POLICIES[policy](), "contracts": contracts}
    expected, _ = _run(tmp_path / "python", blocks, native=False, **options)
    actual, python_runs = _run(
        tmp_path / "rust", blocks, native=True, monkeypatch=monkeypatch, **options
    )
    assert actual == expected
    assert python_runs == []
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    results = {receipt["result"] for receipt in receipts[:-2]}
    assert receipts[-2]["result"] == 300
    if policy == "unmetered":
        assert results == {2000}
    else:
        assert {"'VmRuntimeExecutionError: Out of chi.'", 2000} <= results


LONG = "x" * 600
# (sender, function, kwargs, chi budget) per block; every call goes to con_scan
# except the contract submission.
SCAN_BLOCKS = [
    [
        ("alice", "fill", {"group": "a", "n": 5}, 5000),
        ("alice", "fill", {"group": "b", "n": 3}, 5000),
        ("alice", "scan", {"group": "a"}, 3000),
        ("alice", "drop", {"group": "a", "key": "1"}, 3000),
        ("alice", "scan", {"group": "a"}, 3000),
        ("alice", "scan_everything", {}, 3000),
        ("alice", "info", {"name": "con_scan"}, 3000),
        ("alice", "info", {"name": "con_nope"}, 3000),
        ("alice", "module_info", {"name": "con_probe"}, 3000),
        ("alice", "module_info", {"name": "con_owned"}, 3000),
        ("alice", "module_kind", {"name": "con_probe"}, 3000),
        ("alice", "hashes", {"name": "con_scan", "kind": "ir"}, 3000),
        ("alice", "hashes", {"name": "con_scan", "kind": "source"}, 3000),
        ("alice", "hashes", {"name": "con_scan", "kind": "runtime"}, 3000),
        ("alice", "hashes", {"name": "con_nope", "kind": "source"}, 3000),
        ("alice", "hashes", {"name": "con_scan", "kind": "compiled"}, 3000),
        ("alice", "hashes", {"name": "con_scan", "kind": ""}, 3000),
        ("alice", "submit_contract", {"name": "con_new_1", "code": DEPLOYED}, 100_000),
        ("alice", "info", {"name": "con_new_1"}, 3000),
        ("alice", "hashes", {"name": "con_new_1", "kind": "source"}, 3000),
        ("bob", "foreign", {}, 3000),
        ("alice", "nested_then_scan", {"key": "q"}, 5000),
        ("carol", "scan", {"group": "a"}, 3000),
    ],
    [
        ("alice", "scan", {"group": "a"}, 3000),
        ("alice", "put", {"group": "a", "key": "10", "value": 7}, 3000),
        ("alice", "drop", {"group": "a", "key": "2"}, 3000),
        ("alice", "scan", {"group": "a"}, 3000),
        ("alice", "write_then_scan", {"group": "b"}, 3000),
        ("alice", "clear", {"group": "b"}, 3000),
        ("alice", "scan_everything", {}, 3000),
        ("alice", "clone", {}, 5000),
        ("alice", "clear_then_fail", {"group": "a"}, 3000),
        ("alice", "scan_then_random", {"group": "a"}, 3000),
        ("alice", "scan", {"group": "a"}, 3000),
        ("dave", "foreign", {}, 3000),
        ("bob", "foreign", {}, 3000),
        ("alice", "info", {"name": LONG}, 3000),
        ("alice", "hashes", {"name": LONG, "kind": "ir"}, 3000),
        ("alice", "scan", {"group": LONG}, 3000),
        ("alice", "scan", {"group": "y" * 500}, 3000),
        ("alice", "scan", {"group": "nothing"}, 3000),
    ],
    [
        ("alice", "fill", {"group": "big", "n": 300}, 60_000),
        *[("alice", "scan", {"group": "big"}, budget) for budget in (40, 400, 812, 813, 2000)],
        ("alice", "clear", {"group": "big"}, 300),
        ("alice", "scan_everything", {}, 3000),
        ("alice", "clear", {"group": "big"}, 30_000),
        ("alice", "scan", {"group": "big"}, 3000),
    ],
    [
        ("alice", "fill_text", {"group": "t1", "n": 20, "size": 4000}, 150_000),
        ("alice", "fill_text", {"group": "t2", "n": 20, "size": 4000}, 150_000),
        ("alice", "scan", {"group": "t1"}, 150_000),
        ("alice", "scan_everything", {}, 150_000),
        ("alice", "clear", {"group": "t2"}, 150_000),
    ],
]


def _scan_blocks():
    blocks = []
    for height, calls in enumerate(SCAN_BLOCKS, start=1):
        txs = []
        for index, (sender, function, kwargs, budget) in enumerate(calls):
            contract = "submission" if function == "submit_contract" else "con_scan"
            txs.append(_tx(100 + height, index, height, sender, contract, function, kwargs, budget))
        blocks.append(txs)
    return blocks


def test_hash_scans_and_contract_metadata_run_in_rust_like_the_python_path(tmp_path, monkeypatch):
    blocks = _scan_blocks()
    expected, _ = _run(tmp_path / "python", blocks, native=False)
    actual, python_runs = _run(tmp_path / "rust", blocks, native=True, monkeypatch=monkeypatch)
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert left == right, index
    # Only contract submission and random need Python.
    assert python_runs == [
        ("alice", "submission", "submit_contract"),
        ("alice", "con_scan", "scan_then_random"),
    ]


# Largest and smallest Python timedelta, a negative one and a plain one.
SPANS = [(1, 2), (-3, 5), (999_999_999, 86_399), (-999_999_999, 0)]


def _times_block(height):
    calls = [("keep", {"days": days, "seconds": seconds}) for days, seconds in SPANS]
    calls += [("read", {"days": days}) for days, _ in SPANS]
    calls += [
        ("keep_arg", {"d": {"__delta__": [-1, 86_399]}}),
        ("keep_bytes", {}),
        ("announce", {"seconds": -7_200}),
    ]
    return [
        {
            "payload": {
                "sender": "alice",
                "contract": "con_times",
                "function": function,
                "kwargs": kwargs,
                "nonce": index,
                "chi_supplied": 5000,
            },
            "metadata": {"signature": f"times-{height}-{index}"},
            "b_meta": {
                "nanos": 1_700_000_000_000_000_000 + height,
                "height": height,
                "hash": f"block-{height}",
                "chain_id": "xian-local",
            },
        }
        for index, (function, kwargs) in enumerate(calls)
    ]


def test_timedelta_and_bytearray_values_are_stored_returned_and_emitted(tmp_path, monkeypatch):
    blocks = [_times_block(1)]
    expected, _ = _run(tmp_path / "python", blocks, native=False)
    receipts = [json.loads(receipt) for receipt, _, _ in expected[:-1]]
    assert [receipt["status"] for receipt in receipts] == [0] * len(receipts), receipts
    results = [decode_wire_numbers(receipt["result"]) for receipt in receipts]
    for index, (days, seconds) in enumerate(SPANS):
        span = Timedelta(days=days, seconds=seconds)
        negated = Timedelta(days=-days)
        assert results[index] == span
        # Variable and Hash both read the values back from committed state
        # (the last keep wrote the Variable).
        last = Timedelta(days=SPANS[-1][0], seconds=SPANS[-1][1])
        assert results[len(SPANS) + index] == [last, {"pair": [span, negated]}]
    assert results[-3:-1] == [Timedelta(seconds=-1), bytearray(b"ab\xff")]
    assert receipts[-1]["events"][0]["data"] == {
        "raw": {"__bytearray__": "71"},
        "spans": {"__set__": {"codec": "xian-set-1", "items": [["timedelta", "-7200"]]}},
    }
    assert {"key": "con_times.span", "value": {"__delta__": [-999_999_999, 0]}} in receipts[3][
        "state"
    ]

    actual, python_runs = _run(tmp_path / "rust", blocks, native=True, monkeypatch=monkeypatch)
    assert actual == expected
    # The Rust executor settles these values itself.
    assert python_runs == []


# Every log record the per-transaction path writes (process_tx and the
# functions it calls); "process_block" only notes a hand-back.
PER_TX_STAGES = {"process_tx", "execute_tx", "process_tx_output", "tx_result"}


def _per_tx_logs(home, blocks, *, native, trace_logging, policy):
    client, processor, rewards = _setup(home)
    processor.trace_logging = trace_logging
    records = []

    def capture(message):
        record = message.record
        records.append((record["level"].name, record["message"], dict(record["extra"])))

    sink = logger.add(
        capture, level="TRACE", filter=lambda record: record["extra"].get("stage") in PER_TX_STAGES
    )
    try:
        for txs in blocks:
            if native:
                processor.process_block(txs, fee_policy=policy, rewards_handler=rewards)
            else:
                for tx in txs:
                    processor.process_tx(tx, rewards, fee_policy=policy)
            client.raw_driver.commit()
    finally:
        logger.remove(sink)
        client.raw_driver.close()
    return records


@pytest.mark.parametrize("trace_logging", [True, False])
@pytest.mark.parametrize("policy", ["paid", "free"])
def test_rust_block_execution_writes_the_per_transaction_logs(tmp_path, trace_logging, policy):
    fee_policy = {
        "paid": TxFeePolicy.paid_metered(),
        "free": TxFeePolicy.free_metered(),
    }[policy]
    blocks = [_block(seed, 30, height) for height, seed in enumerate((21, 22), start=1)]
    blocks.append(_scan_blocks()[0])
    expected = _per_tx_logs(
        tmp_path / "python", blocks, native=False, trace_logging=trace_logging, policy=fee_policy
    )
    actual = _per_tx_logs(
        tmp_path / "rust", blocks, native=True, trace_logging=trace_logging, policy=fee_policy
    )
    stages = {record[2]["stage"] for record in expected}
    assert stages >= ({"execute_tx", "tx_result"} if trace_logging else {"process_tx_output"})
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert left == right, index
