"""Cross-SDK transaction signing parity for node admission."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import xian.utils.tx as tx_utils

if os.environ.get("XIAN_SDK_TRANSACTION_PARITY") != "1":
    pytest.skip(
        "set XIAN_SDK_TRANSACTION_PARITY=1 to run cross-SDK parity",
        allow_module_level=True,
    )

CHAIN_ID = "xian-local"
PRIVATE_KEY = "".join(f"{value:02x}" for value in range(32))
ROOT = Path(__file__).resolve().parents[2]
XIAN_PY_DIR = Path(os.environ.get("XIAN_PY_DIR", ROOT.parent / "xian-py"))
XIAN_JS_DIR = Path(os.environ.get("XIAN_JS_DIR", ROOT.parent / "xian-js"))

_deep_value = None
for _ in range(130):
    _deep_value = [_deep_value]

SHARED_REFUSALS = [
    {"name": "reject_unpaired_surrogate", "kwargs": {"data": "\ud800"}},
    {"name": "reject_bad_bytes", "kwargs": {"data": {"__bytes__": "0g"}}},
    {"name": "reject_reserved_tag_with_sibling", "kwargs": {"data": {"__bytes__": "00", "x": 1}}},
    {"name": "reject_bad_delta", "kwargs": {"data": {"__delta__": [1, 2, 3]}}},
    {
        "name": "reject_duplicate_set_member",
        "kwargs": {"data": {"__set__": {"codec": "xian-set-1", "items": [["int", "1"]] * 2}}},
    },
    {
        "name": "reject_unsorted_unicode_set",
        "kwargs": {
            "data": {
                "__set__": {"codec": "xian-set-1", "items": [["str", "😀"], ["str", "\ue000"]]}
            }
        },
    },
    {
        "name": "reject_equal_integer_and_decimal_set_members",
        "kwargs": {
            "data": {
                "__set__": {"codec": "xian-set-1", "items": [["int", "1"], ["fixed", str(10**30)]]}
            }
        },
    },
    {"name": "reject_invalid_datetime", "kwargs": {"data": {"__time__": [1900, 2, 29]}}},
    {"name": "reject_wire_depth", "kwargs": {"data": _deep_value}},
    {"name": "reject_empty_chain_id", "kwargs": {}, "chain_id": ""},
    {"name": "reject_mismatched_sender", "kwargs": {}, "sender": "02" * 32},
]

SHARED_WIRE_VALUES = [
    {"__fixed__": "1"},
    "1",
    {"__fixed__": "0.000000000000000000000000000001"},
    {"__big_int__": str(2**512 - 1)},
    {"__bytes__": "00ff"},
    {"__bytearray__": "00ff"},
    {"__delta__": [2, 3]},
    {"__time__": [2000, 2, 29, 1, 2, 3, 4]},
    {
        "__set__": {
            "codec": "xian-set-1",
            "items": [
                ["none"],
                ["bool", True],
                ["int", "1"],
                ["fixed", "1000000000000000000000000000001"],
                ["str", "\ue000"],
                ["str", "😀"],
                ["bytes", "00ff"],
                ["tuple", [["int", "2"], ["str", "x"]]],
                ["frozenset", [["str", "nested"]]],
            ],
        }
    },
    {"2": "e\u0301", "10": "é", "\ue000": "\ufeff雪", "😀": "emoji"},
]


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    value: dict | None = None
    error: str | None = None


def _run(command: list[str], *, cwd: Path, stdin: str | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        input=stdin,
        text=True,
    )
    return result.stdout


def _python_sdk_records() -> list[dict]:
    script = f"""
import json, sys

from xian_py.transaction import canonical_json, create_tx
from xian_py.wallet import Wallet
from xian_runtime_types.decimal import ContractingDecimal

chain_id = {CHAIN_ID!r}
wallet = Wallet({PRIVATE_KEY!r})
sender = wallet.public_key

cases = [
    {{
        "name": "basic_int_transfer",
        "kwargs": {{"amount": 5, "memo": "ascii memo", "to": sender}},
    }},
    {{
        "name": "unicode_bool_kwargs",
        "kwargs": {{"flag": True, "memo": "snowman: \\u2603", "to": sender}},
    }},
    {{
        "name": "runtime_wrappers",
        "kwargs": {{
            "amount": ContractingDecimal("0.5"),
            "raw": b"abc",
            "to": sender,
            "units": 2**80,
        }},
    }},
    {{
        "name": "reject_float_kwargs",
        "kwargs": {{"amount": 1.5, "to": sender}},
    }},
    {{
        "name": "reject_noncanonical_fixed",
        "kwargs": {{"amount": {{"__fixed__": "1.0"}}}},
    }},
    {{"name": "reject_non_object_kwargs", "kwargs": []}},
    {{
        "name": "reject_boolean_nonce",
        "kwargs": {{"amount": 1, "to": sender}},
        "payload_overrides": {{"nonce": True}},
    }},
] + json.load(sys.stdin)

records = []
for index, case in enumerate(cases, start=1):
    payload = {{
        "chain_id": case.get("chain_id", chain_id),
        "contract": "currency",
        "function": "transfer",
        "kwargs": case["kwargs"],
        "nonce": index,
        "sender": case.get("sender", sender),
        "chi_supplied": 50_000,
    }}
    payload.update(case.get("payload_overrides", {{}}))
    try:
        tx = create_tx(payload, wallet)
    except Exception as exc:
        records.append({{
            "name": case["name"],
            "accepted": False,
            "error": str(exc),
        }})
    else:
        tx_json = canonical_json(tx)
        records.append({{
            "name": case["name"],
            "accepted": True,
            "tx_hex": tx_json.encode("utf-8").hex(),
        }})

print(json.dumps(records, sort_keys=True))
"""
    output = _run(
        ["uv", "run", "--project", str(XIAN_PY_DIR), "python", "-c", script],
        cwd=XIAN_PY_DIR,
        stdin=json.dumps(
            [
                {"name": "shared_wire_values", "kwargs": {"data": SHARED_WIRE_VALUES}},
                *SHARED_REFUSALS,
            ]
        ),
    )
    return json.loads(output)


def _js_sdk_records() -> list[dict]:
    script = f"""
import {{ readFileSync }} from "node:fs";
import {{
  Ed25519Signer,
  XianClient,
  encodeRuntime,
  sortKeysDeep
}} from {str((XIAN_JS_DIR / "packages/client/dist/index.js").resolve())!r};

const chainId = {CHAIN_ID!r};
const signer = new Ed25519Signer({PRIVATE_KEY!r});
const sender = signer.address;
const client = new XianClient({{
  rpcUrl: "http://127.0.0.1:26657",
  chainId,
  fetchFn: async () => {{
    throw new Error("unexpected network call");
  }}
}});

const cases = [
  {{
    name: "basic_int_transfer",
    kwargs: {{ amount: 5, memo: "ascii memo", to: sender }}
  }},
  {{
    name: "unicode_bool_kwargs",
    kwargs: {{ flag: true, memo: "snowman: \\u2603", to: sender }}
  }},
  {{
    name: "runtime_wrappers",
    kwargs: {{
      amount: {{ __fixed__: "0.5" }},
      raw: new Uint8Array([97, 98, 99]),
      to: sender,
      units: 2n ** 80n
    }}
  }},
  {{
    name: "reject_float_kwargs",
    kwargs: {{ amount: 1.5, to: sender }}
  }},
  {{ name: "reject_noncanonical_fixed", kwargs: {{ amount: {{ __fixed__: "1.0" }} }} }},
  {{ name: "reject_non_object_kwargs", kwargs: [] }},
  {{
    name: "reject_boolean_nonce",
    kwargs: {{ amount: 1, to: sender }},
    payloadOverrides: {{ nonce: true }}
  }}
].concat(JSON.parse(readFileSync(0, "utf8")));

const records = [];
for (const [index, testCase] of cases.entries()) {{
  try {{
    const tx = await client.buildTx({{
      sender: testCase.sender ?? sender,
      contract: "currency",
      function: "transfer",
      kwargs: testCase.kwargs,
      nonce: index + 1,
      chi: 50_000,
      chainId: testCase.chain_id ?? chainId,
      ...(testCase.payloadOverrides ?? {{}})
    }});
    const signedTx = await client.signTx(tx, signer);
    const txJson = encodeRuntime(sortKeysDeep(signedTx));
    records.push({{
      name: testCase.name,
      accepted: true,
      tx_hex: Buffer.from(txJson, "utf8").toString("hex")
    }});
  }} catch (error) {{
    records.push({{
      name: testCase.name,
      accepted: false,
      error: String(error?.message ?? error)
    }});
  }}
}}

console.log(JSON.stringify(records));
"""
    output = _run(
        ["node", "--input-type=module", "-e", script],
        cwd=XIAN_JS_DIR,
        stdin=json.dumps(
            [
                {"name": "shared_wire_values", "kwargs": {"data": SHARED_WIRE_VALUES}},
                *SHARED_REFUSALS,
            ]
        ),
    )
    return json.loads(output)


def _validate(raw_tx: bytes, *, chain_id: str = CHAIN_ID) -> ValidationResult:
    try:
        value = tx_utils.decode_and_validate_transaction_static_bytes(
            raw_tx,
            chain_id=chain_id,
        )
    except tx_utils.TransactionException as exc:
        return ValidationResult(accepted=False, error=str(exc))
    return ValidationResult(accepted=True, value=value)


def test_python_and_js_sdk_transactions_pass_node_admission():
    if not XIAN_PY_DIR.exists():
        pytest.fail(f"xian-py checkout not found at {XIAN_PY_DIR}")
    if not XIAN_JS_DIR.exists():
        pytest.fail(f"xian-js checkout not found at {XIAN_JS_DIR}")
    if not (XIAN_JS_DIR / "packages/client/dist/index.js").exists():
        pytest.fail("xian-js client package must be built before this test")

    python_records = {record["name"]: record for record in _python_sdk_records()}
    js_records = {record["name"]: record for record in _js_sdk_records()}

    assert js_records.keys() == python_records.keys()
    for name, python_record in python_records.items():
        js_record = js_records[name]
        assert js_record["accepted"] == python_record["accepted"], name
        assert python_record["accepted"] == (not name.startswith("reject_")), name
        if not python_record["accepted"]:
            continue

        assert js_record["tx_hex"] == python_record["tx_hex"], name
        raw_tx = python_record["tx_hex"].encode("ascii")
        result = _validate(raw_tx)
        assert result.accepted, (name, result.error)
        assert result.value == json.loads(bytes.fromhex(python_record["tx_hex"])), name


def test_default_sdk_signatures_match_v2_fixtures_and_node():
    fixture = (
        ROOT.parent / "xian-contracting/tests/fixtures/transaction_numeric_v2.json"
    ).read_text(encoding="utf-8")
    python_script = """
import json, sys
from xian_py import Wallet
from xian_py.transaction import create_tx, prepare_transaction
from xian_runtime_types.encoding import convert_dict

vectors = json.load(sys.stdin)
wallet = Wallet(vectors["private_key"])
for case in vectors["cases"]:
    payload = case["payload"]
    payload["kwargs"] = convert_dict(payload["kwargs"])
    print(prepare_transaction(create_tx(payload, wallet)).submitted_bytes.decode("ascii"))
"""
    js_script = """
import { readFileSync } from "node:fs";
import { Ed25519Signer, XianClient, prepareTransaction } from "./packages/client/dist/index.js";

const vectors = JSON.parse(readFileSync(0, "utf8"), (_, value) =>
  value && typeof value === "object" && "__big_int__" in value ? BigInt(value.__big_int__) : value
);
const client = new XianClient({ rpcUrl: "http://127.0.0.1" });
const signer = new Ed25519Signer(vectors.private_key);
for (const { payload } of vectors.cases) {
  console.log(prepareTransaction(await client.signTx({ payload }, signer)).rpcHex);
}
"""
    python_hex = _run(
        ["uv", "run", "--project", str(XIAN_PY_DIR), "python", "-c", python_script],
        cwd=XIAN_PY_DIR,
        stdin=fixture,
    ).splitlines()
    js_hex = _run(
        ["node", "--input-type=module", "-e", js_script], cwd=XIAN_JS_DIR, stdin=fixture
    ).splitlines()
    for case, python_wire, js_wire in zip(
        json.loads(fixture)["cases"], python_hex, js_hex, strict=True
    ):
        expected = case["transaction_json"].encode("utf-8").hex()
        assert python_wire == js_wire == expected, case["name"]
        result = _validate(python_wire.encode("ascii"), chain_id=case["payload"]["chain_id"])
        assert result.accepted, result.error
        assert result.value["payload"] == case["payload"]
        assert result.value["metadata"]["signature"] == case["signature"]


def test_sdk_contract_values_keep_their_wire_types_when_reused():
    wire = json.dumps(SHARED_WIRE_VALUES)
    python = _run(
        [
            "uv",
            "run",
            "--project",
            str(XIAN_PY_DIR),
            "python",
            "-c",
            "import sys; from xian_runtime_types.encoding import decode, encode_wire;"
            "print(encode_wire(decode(sys.stdin.read())))",
        ],
        cwd=XIAN_PY_DIR,
        stdin=wire,
    )
    js = _run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import { readFileSync } from "node:fs";
import { decodeRuntime, encodeRuntime } from "./packages/client/dist/index.js";
console.log(encodeRuntime(decodeRuntime(readFileSync(0, "utf8"))));
""",
        ],
        cwd=XIAN_JS_DIR,
        stdin=wire,
    )
    assert json.loads(python) == json.loads(js) == SHARED_WIRE_VALUES
