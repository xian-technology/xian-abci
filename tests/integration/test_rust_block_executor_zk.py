"""zk contracts executed by the Rust block executor against the per-transaction path.

The block executor serves every zk syscall in Rust (vm-core
``block_host/zk.rs``) and refuses a contract that uses zk while the chain's
zk runtime feature is off, as the Python path does. Receipts and pending
state must be byte-identical to processing every transaction with
``process_tx``, and no zk transaction may be handed to the Python path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
from pathlib import Path

import pytest
from contracting.local import ContractingClient
from contracting.local.client import AbstractContract
from contracting.stdlib.bridge import zk as zk_bridge
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.encoding import encode_abci_json
from xian.utils.tx import transaction_bytes

pytest.importorskip("xian_vm_core")

ROOT = Path(__file__).resolve().parents[3]
DEMO = json.loads(
    (ROOT / "xian-contracting/packages/xian-zk/tests/fixtures/groth16_bn254_demo.json").read_text()
)

REGISTRY = """verifying_keys = Hash()

@export
def put(vk_id: str, field: str, value: Any):
    verifying_keys[vk_id, field] = value
"""

ZK_OPS = """@export
def run(ops: list):
    out = []
    for op in ops:
        out.append(apply(op))
    return out

def apply(op: list):
    kind = op[0]
    if kind == "available":
        return zk.is_available()
    if kind == "has":
        return zk.has_verifying_key(op[1])
    if kind == "info":
        return zk.get_vk_info(op[1])
    if kind == "verify":
        return zk.verify_groth16(op[1], op[2], op[3])
    if kind == "verify_raw":
        return zk.verify_groth16_bn254(op[1], op[2], op[3])
    if kind == "warm":
        return zk.warm_verified_proofs(op[1])
    if kind == "append":
        return zk.shielded_note_append_commitments(op[1], op[2], op[3])
    if kind == "digest":
        return zk.shielded_command_nullifier_digest(op[1])
    if kind == "binding":
        return zk.shielded_command_binding(
            op[1], op[2], op[3], op[4], op[5], op[6], op[7], op[8], op[9], op[10]
        )
    if kind == "tag":
        return zk.shielded_command_execution_tag(op[1], op[2])
    if kind == "hash":
        return zk.shielded_output_payload_hash(op[1])
    if kind == "hashes":
        return zk.shielded_output_payload_hashes(op[1])
    if kind == "deposit":
        return zk.shielded_deposit_public_inputs(op[1], op[2], op[3], op[4], op[5])
    if kind == "transfer":
        return zk.shielded_transfer_public_inputs(op[1], op[2], op[3], op[4], op[5])
    if kind == "withdraw":
        return zk.shielded_withdraw_public_inputs(op[1], op[2], op[3], op[4], op[5], op[6], op[7])
    if kind == "command":
        return zk.shielded_command_public_inputs(
            op[1], op[2], op[3], op[4], op[5], op[6], op[7], op[8], op[9]
        )
    zk.clear_verified_proof_cache()
    zk.clear_prepared_vk_cache()
    return None
"""

IMPORTER = """import con_zk

@export
def available():
    return con_zk.run(ops=[["available"]])
"""

INSPECTOR = """@export
def has_export(name: str):
    return importlib.has_export(name, "run")

@export
def dynamic(name: str):
    return importlib.import_module(name).run(ops=[["available"], ["has", "demo"]])
"""

DEPLOYER = """import submission

@export
def deploy_zk(name: str):
    submission.submit_contract(name=name, code="@export\\ndef ok():\\n    return zk.is_available()\\n")
"""


def field(value: int) -> str:
    return "0x" + format(value, "064x")


FIELDS = [field(0), field(1), field(7), "0x" + "ff" * 32, "0x00", 5, None]
NAMES = ["con_token", "", 5]
INTS = [0, 3, -1, {"__big_int__": str(2**64 - 1)}, {"__big_int__": str(2**64)}, True]


def _fields(rng, low=0, high=5):
    return [rng.choice(FIELDS[:3]) for _ in range(rng.randint(low, high))]


def _proof(rng):
    if rng.random() < 0.5:
        return [rng.choice(["demo", "demo", "late", "inactive", "nope", ""]), DEMO["proof_hex"]]
    return [rng.choice(["demo", "garbage", 7]), rng.choice(["0x00", "0x1", DEMO["proof_hex"]])]


def _inputs(rng):
    return rng.choice([DEMO["public_inputs"], DEMO["public_inputs"], [field(9)], [], "0x00"])


def _zk_op(rng):
    kind = rng.choice(
        ["verify"] * 4
        + ["available", "has", "info", "verify_raw", "warm", "append", "digest", "binding"]
        + ["tag", "hash", "hashes", "deposit", "transfer", "withdraw", "command", "clear"]
    )
    if kind in ("has", "info"):
        return [kind, rng.choice(["demo", "late", "inactive", "nope", "", 3])]
    if kind == "verify":
        return [kind, *_proof(rng), _inputs(rng)]
    if kind == "verify_raw":
        return [kind, rng.choice([DEMO["vk_hex"], "0x12", "12"]), _proof(rng)[1], _inputs(rng)]
    if kind == "warm":
        requests = [dict(zip(("vk_id", "proof_hex"), _proof(rng), strict=True)) for _ in range(2)]
        for request in requests:
            request["public_inputs"] = _inputs(rng)
        return [kind, requests]
    if kind == "append":
        subtrees = [field(0)] * rng.choice([20, 20, 2])
        return [kind, rng.choice([0, 1, -1, 2**20]), subtrees, _fields(rng, 0, 2)]
    if kind == "digest":
        return [kind, _fields(rng, 0, 5)]
    if kind == "binding":
        return [kind, *(rng.choice(FIELDS[:3]) for _ in range(8)), rng.choice(INTS), 0]
    if kind == "tag":
        return [kind, rng.choice(FIELDS), rng.choice(FIELDS)]
    if kind == "hash":
        return [kind, rng.choice(["", None, "0x1234", "0x1", 4])]
    if kind == "hashes":
        return [kind, [rng.choice(["", None, "0xabcd"]) for _ in range(rng.randint(0, 3))]]
    commitments = _fields(rng, 0, 3)
    hashes = [field(0) for _ in commitments] if rng.random() < 0.8 else _fields(rng)
    name, root = rng.choice(NAMES), rng.choice(FIELDS[:2] + ["0x00"])
    if kind == "deposit":
        return [kind, name, root, rng.choice(INTS), commitments, hashes]
    if kind == "transfer":
        return [kind, name, root, _fields(rng, 1, 4), commitments, hashes]
    if kind == "withdraw":
        return [kind, name, root, rng.choice(INTS), "bob", _fields(rng, 0, 4), commitments, hashes]
    if kind == "command":
        nullifiers = _fields(rng, 1, 4)
        return [
            kind,
            name,
            root,
            field(3),
            field(4),
            rng.choice(INTS),
            2,
            nullifiers,
            commitments,
            hashes,
        ]
    return [kind]


def _tx(
    seed, index, height, sender, contract, function, kwargs, budget=200_000, chain="xian-local"
):
    return {
        "payload": {
            "sender": sender,
            "contract": contract,
            "function": function,
            "kwargs": kwargs,
            "nonce": index,
            "chi_supplied": budget,
        },
        "metadata": {"signature": f"zk-{seed}-{index}"},
        "b_meta": {
            "nanos": 1_700_000_000_000_000_000 + height,
            "height": height,
            "hash": f"block-{height}",
            "chain_id": chain,
        },
    }


def _registration(index, height, vk_id, fields):
    return [
        _tx(
            height,
            index + offset,
            height,
            "sys",
            "zk_registry",
            "put",
            {"vk_id": vk_id, "field": name, "value": value},
            5_000,
        )
        for offset, (name, value) in enumerate(fields.items())
    ]


def _key(vk_hex=DEMO["vk_hex"], **overrides):
    fields = {"vk_hex": vk_hex, "scheme": "groth16", "curve": "bn254", "active": True}
    fields["vk_hash"] = "hash-" + vk_hex[-8:]
    fields.update(overrides)
    return fields


def _zk_blocks():
    blocks = []
    for height in (1, 2):
        rng = random.Random(600 + height)
        txs = []
        if height == 1:
            # Registered earlier in the block than the calls that use it.
            txs += _registration(0, height, "late", _key())
        for index in range(len(txs), 70):
            ops = [_zk_op(rng) for _ in range(rng.randint(1, 4))]
            txs.append(_tx(600 + height, index, height, "alice", "con_zk", "run", {"ops": ops}))
        blocks.append(txs)
    return blocks


def _disabled_block(height):
    calls = [
        ("con_zk", "run", {"ops": [["available"]]}),
        ("con_zk_importer", "available", {}),
        ("con_inspector", "has_export", {"name": "con_zk"}),
        ("con_inspector", "has_export", {"name": "con_inspector"}),
        ("con_deployer", "deploy_zk", {"name": "con_zk_late"}),
    ]
    return [
        _tx(height, index, height, "alice", contract, function, kwargs)
        for index, (contract, function, kwargs) in enumerate(calls)
    ]


def _dynamic_block(height):
    """A dynamic import is not prepared; its zk calls reach the host."""
    return [_tx(height, 0, height, "alice", "con_inspector", "dynamic", {"name": "con_zk"})]


def _assert_dynamic_zk_call_fails(tmp_path, monkeypatch, blocks, patches, message):
    """Revision 14: with the zk feature off (or unreadable) every zk syscall
    fails, however its contract was reached; the Python path ran them. The
    blocks before the dynamic import's own last block are identical."""
    receipts = _assert_same(tmp_path, monkeypatch, blocks[:-1], patches=patches)
    actual, python_runs = _run(
        tmp_path / "rust-dynamic", blocks, native=True, monkeypatch=monkeypatch, patches=patches
    )
    assert python_runs == []
    dynamic = json.loads(actual[-2])
    assert dynamic["status"] == 1
    assert message in dynamic["result"]
    return receipts


def _setup(home):
    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=home)
    driver = client.raw_driver
    submitted = Datetime(2026, 1, 2, 3, 4, 5)
    driver.set_contract_from_source(
        "currency", (ROOT / "xian-configs/contracts/currency.s.py").read_text(), timestamp=submitted
    )
    for name, source in [
        ("zk_registry", REGISTRY),
        ("con_zk", ZK_OPS),
        ("con_zk_importer", IMPORTER),
        ("con_inspector", INSPECTOR),
        ("con_deployer", DEPLOYER),
    ]:
        driver.set_contract_from_source(name, source, timestamp=submitted)
    keys = {
        "demo": _key(),
        "inactive": _key(active=False),
        "garbage": _key("0x1234"),
    }
    for vk_id, fields in keys.items():
        for name, value in fields.items():
            driver.set(f"zk_registry.verifying_keys:{vk_id}:{name}", value)
    driver.set("zk_registry.verifying_keys:demo:circuit_name", "square")
    for account in ("alice", "sys"):
        driver.set(f"currency.balances:{account}", 10_000_000)
    driver.set("chi_cost.S:value", 20)
    driver.commit()
    return client, TxProcessor(client=client)


def _run(home, blocks, *, native, monkeypatch, patches=None):
    client, processor = _setup(home)
    # The proof caches are the node's: each run starts without them.
    zk_bridge.clear_verified_proof_cache()
    zk_bridge.clear_prepared_vk_cache()
    policy = TxFeePolicy.paid_metered()
    python_runs = []
    process_tx = processor.process_tx

    def counted(tx, *args, **kwargs):
        python_runs.append(tx["payload"]["function"])
        return process_tx(tx, *args, **kwargs)

    monkeypatch.setattr(processor, "process_tx", counted)
    receipts = []
    for height, txs in enumerate(blocks, start=1):
        for key, value in (patches or {}).get(height, {}).items():
            client.raw_driver.set(key, value)
        if native:
            responses = processor.process_block(
                txs, [transaction_bytes(tx) for tx in txs], fee_policy=policy
            )
        else:
            responses = [process_tx(tx, fee_policy=policy) for tx in txs]
        receipts += [encode_abci_json(response["tx_result"]) for response in responses]
        receipts.append(encode(client.raw_driver.pending_items()))
        client.raw_driver.commit()
    client.raw_driver.close()
    return receipts, python_runs


def _assert_same(tmp_path, monkeypatch, blocks, patches=None):
    expected, _ = _run(
        tmp_path / "python", blocks, native=False, monkeypatch=monkeypatch, patches=patches
    )
    actual, python_runs = _run(
        tmp_path / "rust", blocks, native=True, monkeypatch=monkeypatch, patches=patches
    )
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert _digest(left) == _digest(right), index
    assert python_runs == []
    parsed = (json.loads(entry) for entry in expected)
    return [receipt for receipt in parsed if isinstance(receipt, dict)]


def test_zk_syscalls_run_in_rust_like_the_python_path(tmp_path, monkeypatch):
    receipts = _assert_same(tmp_path, monkeypatch, _zk_blocks())
    results = [receipt["result"] for receipt in receipts]
    assert 20 < sum(receipt["status"] == 0 for receipt in receipts) < len(receipts) - 20
    assert any("Unknown verifying key id" in str(result) for result in results)
    assert any("ValueError" in str(result) for result in results)
    assert any("int too big to convert" in str(result) for result in results)


def test_contracts_using_zk_fail_while_zk_is_off(tmp_path, monkeypatch):
    blocks = [_zk_blocks()[0][:12], _disabled_block(2), _dynamic_block(3)]
    receipts = _assert_dynamic_zk_call_fails(
        tmp_path,
        monkeypatch,
        blocks,
        {2: {"__runtime_features.zk": False}},
        "zk.is_available is unavailable: the chain zk runtime feature is disabled",
    )
    disabled = receipts[12:]
    assert [receipt["status"] for receipt in disabled] == [1, 1, 1, 0, 1]
    message = "uses zk host syscalls, but the chain zk runtime feature is disabled"
    assert all(message in disabled[index]["result"] for index in (0, 1, 2, 4))
    assert disabled[0]["chi_used"] == 5


def test_a_feature_value_the_check_refuses_fails_like_the_python_path(tmp_path, monkeypatch):
    blocks = [_disabled_block(1), _dynamic_block(2)]
    receipts = _assert_dynamic_zk_call_fails(
        tmp_path,
        monkeypatch,
        blocks,
        {1: {"__runtime_features.zk": "maybe"}},
        "runtime feature 'zk' must be a boolean",
    )
    assert [receipt["status"] for receipt in receipts] == [1, 1, 1, 0, 1]
    # Preparation fails in the host runtime; the VM's checks raise ValueError.
    assert "Transaction failed in the host runtime." in receipts[0]["result"]
    assert "runtime feature 'zk' must be a boolean" in receipts[2]["result"]


def _digest(record) -> str:
    """A receipt or state encoding's digest (a failed comparison stays short)."""
    data = record if isinstance(record, bytes) else record.encode()
    return hashlib.sha256(data).hexdigest()


def _load_shielded_benchmark():
    path = ROOT / "xian-abci/scripts/benchmark_shielded_chi.py"
    spec = importlib.util.spec_from_file_location("benchmark_shielded_chi", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shielded_run(benchmark, monkeypatch, *, per_tx, sent):
    """The benchmark's shielded note flows on one path: each transaction's
    receipt and the block's pending state. Proofs and note ciphertexts are
    random, so a replay sends the first run's arguments again."""
    replay = list(sent) if sent else None
    records = []
    # The benchmark deploys through the local client, which stamps the
    # wall-clock minute: both runs deploy at one fixed time.
    monkeypatch.setattr(AbstractContract, "now", lambda self: Datetime(2026, 1, 2, 3, 4))

    def process(processor, *, function, sender, kwargs, height, chi=25_000_000):
        kwargs = replay.pop(0) if replay is not None else kwargs
        if replay is None:
            sent.append(kwargs)
        # The relay proof binds the benchmark's chain id.
        tx = _tx(
            height,
            0,
            height,
            sender,
            "con_shielded_note_token",
            function,
            kwargs,
            chi,
            "test-chain",
        )
        policy = TxFeePolicy.paid_metered()
        if per_tx:
            response = processor.process_tx(tx, fee_policy=policy)
        else:
            (response,) = processor.process_block([tx], [transaction_bytes(tx)], fee_policy=policy)
        records.append(encode_abci_json(response["tx_result"]))
        records.append(encode(processor.client.raw_driver.pending_items()))
        assert response["tx_result"]["status"] == 0, response["tx_result"]["result"]
        return int(response["tx_result"]["chi_used"])

    monkeypatch.setattr(benchmark, "process", process)
    return benchmark.benchmark(), records


def test_shielded_note_transactions_run_in_rust_like_the_python_path(monkeypatch):
    pytest.importorskip("xian_zk")
    benchmark = _load_shielded_benchmark()
    sent = []
    python_chi, expected = _shielded_run(benchmark, monkeypatch, per_tx=True, sent=sent)
    process_tx = TxProcessor.process_tx
    python_runs = []

    def counted(self, tx, *args, **kwargs):
        python_runs.append(tx["payload"]["function"])
        return process_tx(self, tx, *args, **kwargs)

    monkeypatch.setattr(TxProcessor, "process_tx", counted)
    rust_chi, actual = _shielded_run(benchmark, monkeypatch, per_tx=False, sent=sent)
    assert python_chi == rust_chi == benchmark.BASELINE
    assert len(actual) == len(expected) == 12
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert _digest(left) == _digest(right), index
    assert python_runs == []
