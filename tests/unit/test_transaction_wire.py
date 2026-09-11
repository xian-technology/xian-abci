import json
from pathlib import Path

import pytest

from xian.utils import tx as tx_module

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "xian-contracting/tests/fixtures/transaction_wire.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("native", [False, True], ids=["python", "native"])
@pytest.mark.parametrize("vector", VECTORS["cases"], ids=lambda item: item["name"])
def test_shared_sdk_transactions_pass_node_validation(monkeypatch, vector, native):
    if native and tx_module._native_decode_and_validate_transaction_static is None:
        pytest.skip("native fastpath extension is not installed")
    if not native:
        monkeypatch.setattr(tx_module, "_native_decode_and_validate_transaction_static", None)
    wire = vector["transaction_json"].encode().hex().encode()
    decoded = tx_module.decode_and_validate_transaction_static_bytes(
        wire, chain_id=vector["payload"]["chain_id"]
    )
    assert decoded["payload"] == vector["payload"]
