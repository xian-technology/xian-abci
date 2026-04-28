"""Cross-SDK transaction signing parity for node admission."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

import xian.utils.tx as tx_utils

if os.environ.get("XIAN_SDK_TRANSACTION_PARITY") != "1":
    pytest.skip(
        "set XIAN_SDK_TRANSACTION_PARITY=1 to run cross-SDK parity",
        allow_module_level=True,
    )

xian_fastpath_core = pytest.importorskip("xian_fastpath_core")
_native_decode_static = (
    xian_fastpath_core.decode_and_validate_transaction_static
)

CHAIN_ID = "xian-local"
PRIVATE_KEY = "".join(f"{value:02x}" for value in range(32))
ROOT = Path(__file__).resolve().parents[2]
XIAN_PY_DIR = Path(os.environ.get("XIAN_PY_DIR", ROOT.parent / "xian-py"))
XIAN_JS_DIR = Path(os.environ.get("XIAN_JS_DIR", ROOT.parent / "xian-js"))


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    value: dict | None = None
    error: str | None = None


def _run(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _python_sdk_records() -> list[dict]:
    script = f"""
import json

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
    {{"name": "reject_non_object_kwargs", "kwargs": []}},
    {{
        "name": "reject_boolean_nonce",
        "kwargs": {{"amount": 1, "to": sender}},
        "payload_overrides": {{"nonce": True}},
    }},
]

records = []
for index, case in enumerate(cases, start=1):
    payload = {{
        "chain_id": chain_id,
        "contract": "currency",
        "function": "transfer",
        "kwargs": case["kwargs"],
        "nonce": index,
        "sender": sender,
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
    )
    return json.loads(output)


def _js_sdk_records() -> list[dict]:
    script = f"""
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
  {{ name: "reject_non_object_kwargs", kwargs: [] }},
  {{
    name: "reject_boolean_nonce",
    kwargs: {{ amount: 1, to: sender }},
    payloadOverrides: {{ nonce: true }}
  }}
];

const records = [];
for (const [index, testCase] of cases.entries()) {{
  try {{
    const tx = await client.buildTx({{
      sender,
      contract: "currency",
      function: "transfer",
      kwargs: testCase.kwargs,
      nonce: index + 1,
      chi: 50_000,
      chainId,
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
        ["node", "--input-type=module", "-e", script], cwd=XIAN_JS_DIR
    )
    return json.loads(output)


@contextmanager
def _validation_mode(*, native: bool):
    original_decode_static = (
        tx_utils._native_decode_and_validate_transaction_static
    )
    tx_utils._native_decode_and_validate_transaction_static = (
        _native_decode_static if native else None
    )
    try:
        yield
    finally:
        tx_utils._native_decode_and_validate_transaction_static = (
            original_decode_static
        )


def _validate(raw_tx: bytes, *, native: bool) -> ValidationResult:
    with _validation_mode(native=native):
        try:
            value = tx_utils.decode_and_validate_transaction_static_bytes(
                raw_tx,
                chain_id=CHAIN_ID,
            )
        except Exception as exc:
            return ValidationResult(accepted=False, error=str(exc))
    return ValidationResult(accepted=True, value=value)


def test_python_and_js_sdk_transactions_match_admission_fastpath():
    if not XIAN_PY_DIR.exists():
        pytest.fail(f"xian-py checkout not found at {XIAN_PY_DIR}")
    if not XIAN_JS_DIR.exists():
        pytest.fail(f"xian-js checkout not found at {XIAN_JS_DIR}")
    if not (XIAN_JS_DIR / "packages/client/dist/index.js").exists():
        pytest.fail("xian-js client package must be built before this test")

    python_records = {
        record["name"]: record for record in _python_sdk_records()
    }
    js_records = {record["name"]: record for record in _js_sdk_records()}

    assert js_records.keys() == python_records.keys()
    for name, python_record in python_records.items():
        js_record = js_records[name]
        assert js_record["accepted"] == python_record["accepted"], name
        if not python_record["accepted"]:
            continue

        assert js_record["tx_hex"] == python_record["tx_hex"], name
        raw_tx = python_record["tx_hex"].encode("ascii")
        python_result = _validate(raw_tx, native=False)
        native_result = _validate(raw_tx, native=True)
        assert python_result.accepted, name
        assert native_result.accepted == python_result.accepted, name
        assert native_result.value == python_result.value, name
