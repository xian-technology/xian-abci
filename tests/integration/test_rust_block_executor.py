"""Blocks executed by the Rust block executor.

``TxProcessor.process_block`` runs a block's transactions in Rust. Until
increment 9 of the single-engine plan every scenario here was also run on the
Python per-transaction path and compared byte for byte; those outputs were
frozen as golden fixtures (``golden_support``) and the receipts, responses,
pending state and log records must keep reproducing them.
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

from xian.utils.tx import transaction_bytes
from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.encoding import encode_abci_json
from golden_support import assert_matches_golden

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
def big_ints(n: int):
    return [1152921504606846976 + i for i in range(n)]

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
RANDOM = """import con_probe
draws = Hash()

@export
def draw(salt: Any, ops: list):
    if salt != "unseeded":
        random.seed(salt)
    out = []
    for op in ops:
        out.append(apply(op))
    draws[ctx.signer] = out
    return out

def apply(op: list):
    name = op[0]
    if name == "bits":
        return random.getrandbits(op[1])
    if name == "range":
        return random.randrange(op[1])
    if name == "int":
        return random.randint(op[1], op[2])
    if name == "choice":
        return random.choice(op[1])
    if name == "choices":
        return random.choices(op[1], k=op[2])
    if name == "shuffle":
        items = op[1]
        random.shuffle(items)
        return items
    if name == "reseed":
        random.seed(aux_salt=op[1])
        return None
    if name == "nested":
        return con_probe.lucky()
    return random.random()
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
    driver.set_contract_from_source("con_random", RANDOM, timestamp=submitted)
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
    return client, TxProcessor(client=client)


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
# Entry rejections once the fee is reserved: an owner rejection (bob owns
# con_owned), a function that is not exported.
REJECTED = {("con_owned", "ping"), ("con_probe", "missing")}


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


def _run(home, blocks, *, policy=None, contracts=()):
    client, processor = _setup(home, contracts)
    policy = policy or TxFeePolicy.paid_metered()
    receipts = []
    for txs in blocks:
        responses = processor.process_block(
            txs, [transaction_bytes(tx) for tx in txs], fee_policy=policy
        )
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
    return receipts


def test_rust_block_execution_reproduces_the_frozen_receipts(tmp_path):
    blocks = [_block(seed, 40, height) for height, seed in enumerate((11, 12, 13), start=1)]
    actual = _run(tmp_path, blocks)
    assert_matches_golden("block_executor/paid_blocks", actual)
    # Failures pay their fee out too, also to a validator set changed earlier
    # in the block.
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    paid_failures = [r for r in receipts if r["status"] == 1 and r["rewards"]]
    assert len(paid_failures) > 10
    assert any("val3" in r["rewards"]["validator_reward"] for r in paid_failures)

    # The entry rejections are receipts too.
    rejected = [r for r in receipts if "not exported" in str(r["result"]) or "owner" in str(r["result"])]
    assert len(rejected) > 5


def test_unmetered_system_execution_reproduces_the_frozen_receipts(tmp_path):
    # System transactions (epoch rebalance, evidence penalties) run in Rust
    # unmetered: no chi budget (burn's 40 chi do not run out), no failure
    # charge, no fee, no rewards.
    policy = TxFeePolicy.system_unmetered()
    blocks = [_block(seed, 40, height) for height, seed in enumerate((31, 32), start=1)]
    actual = _run(tmp_path, blocks, policy=policy)
    assert_matches_golden("block_executor/unmetered_blocks", actual)
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    assert {receipt["status"] for receipt in receipts} == {0, 1}
    assert not any(receipt["rewards"] for receipt in receipts)
    # Without a fee to reserve, every sender reaches the entry rejections
    # (reported with the base chi).
    calls = [(tx["payload"]["contract"], tx["payload"]["function"]) for txs in blocks for tx in txs]
    rejected = [r for r, call in zip(receipts, calls, strict=True) if call in REJECTED]
    assert rejected and all(r["chi_used"] == 5 for r in rejected if r["status"] == 1)


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
def test_contract_metadata_changes_reproduce_the_frozen_receipts(tmp_path, policy):
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
    actual = _run(
        tmp_path, blocks, **options
    )
    assert_matches_golden(f"block_executor/metadata_{policy}", actual)
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    results = {receipt["result"] for receipt in receipts[:-2]}
    assert receipts[-2]["result"] == 300
    if policy == "unmetered":
        assert results == {2000}
    else:
        assert {"'VmRuntimeExecutionError: Out of chi.'", 2000} <= results


# A submission contract that deploys without naming the developer, deployer
# or initiator: the host takes them from the transaction (signer and caller),
# not from the calling frame.
DEPLOYER = """@export
def make(name: str, code: str, args: dict = None, owner: str = None):
    Contract.deploy(name=name, code=code, constructor_args=args, owner=owner)
    return Contract.get_info(name)
"""
CONSTRUCTED = """v = Variable()
seen = Variable()

@construct
def seed(start: int = 1, amount: decimal = None, items: list = None, nested: dict = None):
    assert start > 0, "bad start"
    v.set(start)
    # While it constructs, the module loads as the contract but the contract
    # does not exist yet and has no code hash.
    seen.set([
        ctx.caller, ctx.signer, ctx.owner, ctx.this, amount, items, nested,
        importlib.exists(ctx.this), importlib.has_export(ctx.this, "get"),
        importlib.code_hash(ctx.this),
    ])

@export
def get():
    return [v.get(), seen.get()]
"""
RELAY_DEPLOY = """import submission

@export
def relay(name: str, code: str):
    return submission.make(name=name, code=code)
"""


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_deployments_reproduce_the_frozen_receipts(tmp_path, policy):
    # Budgets from too small for the call to enough for the deployment: each
    # failure point (VM, constructor, metadata writes, deployment cost) is
    # crossed. Then constructor arguments, owners and a relayed deployment.
    calls = [
        ("alice", "submission", "make", {"name": f"con_b{budget}", "code": DEPLOYED}, budget)
        for budget in range(20, 420, 5)
    ]
    arguments = {
        "start": 3,
        "amount": {"__fixed__": "1.5"},
        "items": [1, "a", [True, None]],
        "nested": {"k": {"j": [2]}},
    }
    calls += [
        (
            "alice",
            "submission",
            "make",
            {"name": "con_a1", "code": CONSTRUCTED, "args": arguments},
            50_000,
        ),
        (
            "alice",
            "submission",
            "make",
            {"name": "con_a2", "code": CONSTRUCTED, "args": {"start": 0}},
            50_000,
        ),
        (
            "alice",
            "submission",
            "make",
            {"name": "con_a3", "code": CONSTRUCTED, "owner": "bob"},
            50_000,
        ),
        ("alice", "con_relay", "relay", {"name": "con_a4", "code": CONSTRUCTED}, 50_000),
        ("alice", "con_a1", "get", {}, 3000),
        ("bob", "con_a3", "get", {}, 3000),
        ("alice", "con_a4", "get", {}, 3000),
    ]
    blocks = [[_tx(400, index, 1, *call) for index, call in enumerate(calls)]]
    contracts = [("submission", DEPLOYER, "sub_dev"), ("con_relay", RELAY_DEPLOY, "relay_dev")]
    options = {"policy": POLICIES[policy](), "contracts": contracts}
    actual = _run(
        tmp_path, blocks, **options
    )
    assert_matches_golden(f"block_executor/deployments_{policy}", actual)
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    statuses = [receipt["status"] for receipt in receipts]
    assert statuses[-7:] == [0, 1, 0, 0, 0, 0, 0]
    if policy != "unmetered":
        failures = {receipt["result"] for receipt in receipts[:80] if receipt["status"]}
        assert len(failures) > 2, failures


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


def test_hash_scans_and_contract_metadata_reproduce_the_frozen_receipts(tmp_path):
    blocks = _scan_blocks()
    actual = _run(tmp_path, blocks)
    assert_matches_golden("block_executor/scans", actual)
    # Nothing needs Python.


def test_entry_results_are_charged_in_their_v2_encoding(tmp_path):
    # Ints of 54 to 63 bits leave the VM as {"__big_int__": ...}; the result
    # charge counts those bytes on both paths.
    txs = [
        _tx(31, index, 1, "alice", "con_probe", "big_ints", {"n": n}, 5000)
        for index, n in enumerate((1, 200))
    ]
    actual = _run(tmp_path, [txs])
    assert_matches_golden("block_executor/v2_entry_results", actual)


# Seed salts the Rust host serves, and salts the VM refuses (str/int/None only).
RANDOM_SALTS = ["abc", "", 7, -3, True, False, None, "unseeded"]
REFUSED_SALTS = [{"__fixed__": "1.5"}, [1, "a"], {"a": 1}]
POPULATIONS = [[], [1], [1, "a", {"x": 1}, [2]], list(range(9)), "", "héllo", "ab"]


def _wire(number):
    return number if abs(number) < 2**53 else {"__big_int__": str(number)}


def _random_op(rng):
    kind = rng.choice(
        ["bits"] * 4 + ["range", "int", "choice", "choices", "shuffle", "reseed", "nested", "float"]
    )
    if kind == "bits":
        return [kind, _wire(rng.choice([*range(0, 80), 256, 511, 512, 513, 600, -1, 2**40]))]
    if kind == "range":
        return [kind, _wire(rng.choice([-1, 0, 1, 2, 10, 1000, 2**64, 2**511]))]
    if kind == "int":
        low = rng.choice([-(2**200), -5, 0, 3, 7])
        return [kind, _wire(low), _wire(low + rng.choice([-1, 0, 1, 6, 2**100]))]
    if kind in ("choice", "choices"):
        draws = rng.choice([-1, 0, 1, 3, True, 128 * 1024 + 1, 2**40])
        return [kind, rng.choice(POPULATIONS), _wire(draws)]
    if kind == "shuffle":
        return [kind, list(range(rng.randrange(0, 12)))]
    if kind == "reseed":
        return [kind, rng.choice(RANDOM_SALTS[:-1])]
    return [kind]


def _random_blocks():
    blocks = []
    for height in (1, 2, 3):
        rng = random.Random(500 + height)
        txs = []
        for index in range(60):
            salts = REFUSED_SALTS if rng.random() < 0.08 else RANDOM_SALTS
            kwargs = {
                "salt": rng.choice(salts),
                "ops": [_random_op(rng) for _ in range(rng.randrange(1, 10))],
            }
            txs.append(
                _tx(500 + height, index, height, "alice", "con_random", "draw", kwargs, 20_000)
            )
        blocks.append(txs)
    return blocks


def test_random_reproduces_the_frozen_receipts(tmp_path):
    blocks = _random_blocks()
    actual = _run(tmp_path, blocks)
    assert_matches_golden("block_executor/random", actual)
    receipts = [json.loads(entry[0]) for entry in actual if isinstance(entry, tuple)]
    assert 40 < sum(receipt["status"] == 0 for receipt in receipts) < len(receipts)
    assert any("random.seed() salt must be" in receipt["result"] for receipt in receipts)
    # The VM refuses what is out of bounds.
    assert any("exceeds the maximum allowed allocation" in receipt["result"] for receipt in receipts)


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


def test_timedelta_and_bytearray_values_are_stored_returned_and_emitted(tmp_path):
    actual = _run(tmp_path, [_times_block(1)])
    assert_matches_golden("block_executor/times", actual)
    receipts = [json.loads(receipt) for receipt, _, _ in actual[:-1]]
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


# Every per-transaction log record (the stages the per-transaction path
# wrote, which the block executor's responses keep).
PER_TX_STAGES = {"process_tx", "execute_tx", "process_tx_output", "tx_result"}


def _per_tx_logs(home, blocks, *, trace_logging, policy):
    client, processor = _setup(home)
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
            processor.process_block(txs, [transaction_bytes(tx) for tx in txs], fee_policy=policy)
            client.raw_driver.commit()
    finally:
        logger.remove(sink)
        client.raw_driver.close()
    return records


@pytest.mark.parametrize("trace_logging", [True, False])
@pytest.mark.parametrize("policy", ["paid", "free"])
def test_block_execution_writes_the_per_transaction_logs(tmp_path, trace_logging, policy):
    fee_policy = {
        "paid": TxFeePolicy.paid_metered(),
        "free": TxFeePolicy.free_metered(),
    }[policy]
    blocks = [_block(seed, 30, height) for height, seed in enumerate((21, 22), start=1)]
    blocks.append(_scan_blocks()[0])
    actual = _per_tx_logs(tmp_path, blocks, trace_logging=trace_logging, policy=fee_policy)
    name = f"block_executor/logs_{policy}_{'trace' if trace_logging else 'plain'}"
    assert_matches_golden(name, actual)
    stages = {record[2]["stage"] for record in actual}
    assert stages >= ({"execute_tx", "tx_result"} if trace_logging else {"process_tx_output"})


# Values the receipt encoding would refuse (binary floats, raw decimals,
# decimal tuples, heap references): a V2 contract cannot produce them in its
# effects or result, so the executor needs no check of its own for them.
UNENCODABLE = """v = Variable()
E = LogEvent("E", {"x": (int, str)})

@export
def store_float():
    random.seed()
    v.set(random.random())

@export
def emit_float():
    random.seed()
    E({"x": random.random()})

@export
def return_float():
    random.seed()
    return random.random()

@export
def store_tuple():
    v.set(decimal("1.5").as_tuple())

@export
def store_cycle():
    items = []
    items.append(items)
    v.set(items)
"""


def test_values_the_receipt_encoding_refuses_never_reach_the_effects(tmp_path):
    functions = ["store_float", "emit_float", "return_float", "store_tuple", "store_cycle"]
    txs = [
        _tx(9, index, 1, "alice", "con_unencodable", function, {}, 2_000)
        for index, function in enumerate(functions)
    ]
    contracts = [("con_unencodable", UNENCODABLE, None)]
    actual = _run(
        tmp_path, [txs], contracts=contracts
    )
    assert_matches_golden("block_executor/unencodable", actual)
    receipts = [json.loads(receipt) for receipt, _, _ in actual[:-1]]
    refusals = [
        "binary float and raw Decimal are unavailable in xian_vm_v2",
        "binary float and raw Decimal are unavailable in xian_vm_v2",
        "binary float and raw Decimal are unavailable in xian_vm_v2",
        "has no native attribute 'as_tuple'",
        "cyclic value cannot cross a VM boundary",
    ]
    for receipt, refusal in zip(receipts, refusals, strict=True):
        assert receipt["status"] == 1
        assert refusal in receipt["result"]
        assert not any(write["key"].startswith("con_unencodable.") for write in receipt["state"])
