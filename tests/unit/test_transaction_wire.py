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


@pytest.mark.parametrize("vector", VECTORS["cases"], ids=lambda item: item["name"])
def test_shared_sdk_transactions_pass_node_validation(vector):
    wire = vector["transaction_json"].encode().hex().encode()
    decoded = tx_module.decode_and_validate_transaction_static_bytes(
        wire, chain_id=vector["payload"]["chain_id"]
    )
    assert decoded["payload"] == vector["payload"]


def _signed_wire(
    *, upper_sender: bool = False, upper_signature: bool = False, kwargs=None
) -> bytes:
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
        "kwargs": kwargs or {"amount": 1, "to": "bob"},
        "nonce": 0,
        "sender": sender,
    }
    signature = signing_key.sign(tx_module.canonical_json(payload).encode()).signature.hex()
    if upper_signature:
        signature = signature.upper()
    tx = {"metadata": {"signature": signature}, "payload": payload}
    return tx_module.canonical_json(tx).encode().hex().encode()


@pytest.mark.parametrize(
    "upper_sender, upper_signature",
    [(True, False), (False, True)],
    ids=["sender", "signature"],
)
def test_only_canonical_lowercase_hex_identities_are_admitted(upper_sender, upper_signature):
    # Uppercase hex would let one key act as a second, distinct account.
    canonical = _signed_wire(upper_sender=False, upper_signature=False)
    assert tx_module.decode_and_validate_transaction_static_bytes(canonical, chain_id="xian-local")
    wire = _signed_wire(upper_sender=upper_sender, upper_signature=upper_signature)
    with pytest.raises(tx_module.TransactionException):
        tx_module.decode_and_validate_transaction_static_bytes(wire, chain_id="xian-local")


@pytest.mark.parametrize(
    "delta",
    ["12", "\u0661\u0662", {"10": 0, "-5": 0}],
    ids=["two-character-string", "non-ascii-digits", "two-key-object"],
)
def test_a_delta_argument_is_admitted_only_as_two_integers(delta):
    # CheckTx, ProcessProposal and FinalizeBlock share this admission. Since
    # execution revision 14 a delta that tuple unpacking once read as
    # Timedelta(days, seconds) is not canonical wire input.
    admit = tx_module.decode_and_validate_transaction_static_bytes
    canonical = _signed_wire(kwargs={"d": {"__delta__": [1, 2]}})
    assert admit(canonical, chain_id="xian-local")["payload"]["kwargs"] == {
        "d": {"__delta__": [1, 2]}
    }
    wire = _signed_wire(kwargs={"d": {"__delta__": delta}})
    with pytest.raises(tx_module.TransactionException, match="not canonical"):
        admit(wire, chain_id="xian-local")


@pytest.mark.parametrize("extra", ["zzz", "aaa", "b_meta"])
def test_only_metadata_and_payload_are_admitted(extra):
    # Admission revision 3: unsigned keys beside them would change the
    # transaction hash and the block bytes without the sender's consent.
    tx = json.loads(bytes.fromhex(_signed_wire().decode()))
    tx[extra] = {"height": 1}
    wire = tx_module.canonical_json(tx).encode().hex().encode()
    with pytest.raises(tx_module.TransactionException, match="unexpected or missing keys"):
        tx_module.decode_and_validate_transaction_static_bytes(wire, chain_id="xian-local")
