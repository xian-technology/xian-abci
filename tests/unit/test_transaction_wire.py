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


def _signed_wire(*, upper_sender: bool, upper_signature: bool) -> bytes:
    import nacl.encoding
    import nacl.signing

    signing_key = nacl.signing.SigningKey(bytes(range(32)))
    sender = signing_key.verify_key.encode(encoder=nacl.encoding.HexEncoder).decode()
    if upper_sender:
        sender = sender.upper()
    payload = {
        "chain_id": "xian-local",
        "chi_supplied": 100,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": 1, "to": "bob"},
        "nonce": 0,
        "sender": sender,
    }
    signature = signing_key.sign(tx_module.canonical_json(payload).encode()).signature.hex()
    if upper_signature:
        signature = signature.upper()
    tx = {"metadata": {"signature": signature}, "payload": payload}
    return tx_module.canonical_json(tx).encode().hex().encode()


@pytest.mark.parametrize("native", [False, True], ids=["python", "native"])
@pytest.mark.parametrize(
    "upper_sender, upper_signature",
    [(True, False), (False, True)],
    ids=["sender", "signature"],
)
def test_only_canonical_lowercase_hex_identities_are_admitted(
    monkeypatch, native, upper_sender, upper_signature
):
    # Uppercase hex would let one key act as a second, distinct account.
    if native and tx_module._native_decode_and_validate_transaction_static is None:
        pytest.skip("native fastpath extension is not installed")
    if not native:
        monkeypatch.setattr(tx_module, "_native_decode_and_validate_transaction_static", None)
    canonical = _signed_wire(upper_sender=False, upper_signature=False)
    assert tx_module.decode_and_validate_transaction_static_bytes(canonical, chain_id="xian-local")
    wire = _signed_wire(upper_sender=upper_sender, upper_signature=upper_signature)
    with pytest.raises(tx_module.TransactionException):
        tx_module.decode_and_validate_transaction_static_bytes(wire, chain_id="xian-local")


@pytest.mark.parametrize(
    "sender, signature",
    [("AB" * 32, "cd" * 64), ("ab" * 32, "CD" * 64), ("ab" * 31, "cd" * 64)],
    ids=["upper-sender", "upper-signature", "short-sender"],
)
def test_native_decode_result_is_held_to_the_canonical_identity_rule(sender, signature):
    # Applied after the native fastpath so admission never depends on the
    # installed extension's own hex-case rule.
    tx_module._require_canonical_identity(
        {"payload": {"sender": "ab" * 32}, "metadata": {"signature": "cd" * 64}}
    )
    with pytest.raises(tx_module.TransactionException):
        tx_module._require_canonical_identity(
            {"payload": {"sender": sender}, "metadata": {"signature": signature}}
        )
