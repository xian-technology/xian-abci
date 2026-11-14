"""The transaction admission parity corpus: raw transaction bytes and what
static admission does with them.

``cases()`` builds the corpus deterministically (fixed signing keys, a seeded
generator): hand-written edge cases, the shared SDK vectors, generated
payloads covering every runtime tag with canonical and non-canonical
payloads, key-set and identity variants, raw-level corruption and byte
mutations of a valid transaction. ``outcome()`` runs the node's admission
(``decode_and_validate_transaction_static_bytes``) on one case: the decoded
transaction (as a digest) or the error text. The golden outcomes in
``tests/fixtures/admission_parity.json`` freeze them; moving admission between
implementations must not change a single one.

Regenerate (only with an approved admission revision change):

    python tests/unit/admission_corpus.py --write
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from collections.abc import Iterator
from pathlib import Path

import nacl.signing

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
FIXTURE = ROOT / "tests/fixtures/admission_parity.json"
CHAIN_ID = "xian-local"
SEED = 20261114
SIGNING_KEY = nacl.signing.SigningKey(bytes(range(32)))
SENDER = SIGNING_KEY.verify_key.encode().hex()
TAGS = (
    "__fixed__",
    "__big_int__",
    "__time__",
    "__delta__",
    "__bytes__",
    "__bytearray__",
    "__set__",
    "__frozenset__",
)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def hex_bytes(text: str) -> bytes:
    return text.encode("utf-8").hex().encode()


def base_payload(**overrides) -> dict:
    payload = {
        "chain_id": CHAIN_ID,
        "chi_supplied": 100,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": 1, "to": SENDER},
        "nonce": 0,
        "sender": SENDER,
    }
    payload.update(overrides)
    return payload


def sign(payload: dict, signer: nacl.signing.SigningKey = SIGNING_KEY) -> str:
    """The signature over the payload's signed fields, as the SDKs sign."""
    signed = {
        key: payload[key]
        for key in ("chain_id", "contract", "function", "kwargs", "nonce", "sender", "chi_supplied")
        if key in payload
    }
    return signer.sign(canonical(signed).encode("utf-8")).signature.hex()


def signed_tx(payload: dict, *, metadata_extra=None, top_extra=None) -> bytes:
    metadata = {"signature": sign(payload), **(metadata_extra or {})}
    return hex_bytes(canonical({"metadata": metadata, "payload": payload, **(top_extra or {})}))


def raw_signed_text(kwargs_text: str, **overrides) -> bytes:
    """A signed transaction whose kwargs JSON is spliced in as text."""
    payload = base_payload(kwargs="__KWARGS__", **overrides)
    message = canonical(payload).replace('"__KWARGS__"', kwargs_text)
    signature = SIGNING_KEY.sign(message.encode("utf-8")).signature.hex()
    return hex_bytes('{"metadata":{"signature":"' + signature + '"},"payload":' + message + "}")


def _kwargs_case(name: str, kwargs, **overrides) -> tuple[str, bytes]:
    return name, signed_tx(base_payload(kwargs=kwargs, **overrides))


def _handwritten() -> Iterator[tuple[str, bytes]]:
    yield "valid/transfer", signed_tx(base_payload())
    payload = base_payload()
    tx = {"metadata": {"signature": sign(payload)}, "payload": payload}
    yield "raw/json_dumps_spacing", hex_bytes(json.dumps(tx))
    yield "raw/unsorted_keys", hex_bytes(json.dumps(tx, separators=(",", ":")))
    yield "raw/trailing_newline", hex_bytes(canonical(tx) + "\n")
    yield "raw/leading_space", hex_bytes(" " + canonical(tx))
    yield "raw/bom", hex_bytes("﻿" + canonical(tx))
    yield "raw/uppercase_hex", hex_bytes(canonical(tx)).upper()
    yield "raw/odd_hex", hex_bytes(canonical(tx))[:-1]
    yield "raw/non_hex", b"zz" + hex_bytes(canonical(tx))[2:]
    yield "raw/not_utf8_outer", b"\xff\xfe"
    yield "raw/invalid_utf8_inner", b"7b22ff227d"
    yield "raw/empty", b""
    yield "raw/json_array", hex_bytes("[]")
    yield "raw/json_string", hex_bytes('"x"')
    yield "raw/json_null", hex_bytes("null")
    yield "raw/truncated", hex_bytes(canonical(tx)[:-1])
    yield "raw/nan", raw_signed_text('{"amount":NaN}')
    yield "raw/infinity", raw_signed_text('{"amount":Infinity}')
    for token in ("-0", "1.0", "1e3", "1E3", "0.5", "1.0000000000000001", "1e-400", "01", "+1",
                  "9007199254740991", "9007199254740992", "-9007199254740991",
                  "-9007199254740992", "18446744073709551615", "18446744073709551616",
                  "-9223372036854775808", "-9223372036854775809", "1" * 400):
        yield f"number/{token[:24]}", raw_signed_text('{"amount":' + token + ',"to":"bob"}')
    yield "raw/duplicate_kwargs_key", raw_signed_text('{"amount":1,"amount":2}')
    yield "raw/duplicate_payload", hex_bytes(
        '{"payload":' + canonical(payload) + ',"metadata":{"signature":"'
        + sign(payload) + '"},"payload":' + canonical(payload) + "}"
    )
    yield "raw/lone_surrogate", raw_signed_text('{"memo":"\\ud800"}')
    yield "raw/escaped_ascii", raw_signed_text('{"memo":"\\u0041"}')
    yield "raw/escaped_slash", raw_signed_text('{"memo":"a\\/b"}')
    yield "raw/control_escape", raw_signed_text('{"memo":"\\u0001"}')
    yield "raw/raw_control_char", raw_signed_text('{"memo":"\x01"}')
    yield "raw/del_char", raw_signed_text('{"memo":"\x7f"}')
    yield "raw/tab_escape", raw_signed_text('{"memo":"\\t"}')
    for depth in (120, 125, 126, 127, 128, 129, 130, 200):
        value: object = "leaf"
        for _ in range(depth):
            value = {"next": value}
        yield f"depth/dict_{depth}", signed_tx(base_payload(kwargs={"nested": value}))
        value = "leaf"
        for _ in range(depth):
            value = [value]
        yield f"depth/list_{depth}", signed_tx(base_payload(kwargs={"nested": value}))
    yield "signature/zeroed", hex_bytes(canonical({"metadata": {"signature": "00" * 64},
                                                    "payload": payload}))
    yield "signature/other_key", hex_bytes(canonical({
        "metadata": {"signature": sign(payload, nacl.signing.SigningKey(bytes(32)))},
        "payload": payload,
    }))
    for public_key in ("01" + "00" * 31, "00" * 32, "ec" + "ff" * 30 + "7f", "ee" + "ff" * 30 + "7f"):
        forged = base_payload(sender=public_key)
        yield f"signature/low_order_{public_key[:4]}", hex_bytes(canonical({
            "metadata": {"signature": "01" + "00" * 63}, "payload": forged}))
    signature = bytes.fromhex(sign(payload))
    order = 2**252 + 27742317777372353535851937790883648493
    scalar = int.from_bytes(signature[32:], "little") + order
    yield "signature/noncanonical_scalar", hex_bytes(canonical({
        "metadata": {"signature": (signature[:32] + scalar.to_bytes(32, "little")).hex()},
        "payload": payload,
    }))


def _key_sets() -> Iterator[tuple[str, bytes]]:
    payload = base_payload()
    signature = sign(payload)
    yield "keys/top_extra", signed_tx(payload, top_extra={"zzz": 1})
    yield "keys/top_extra_first", signed_tx(payload, top_extra={"aaa": {"x": 1}})
    yield "keys/top_extra_b_meta", signed_tx(payload, top_extra={"b_meta": {"height": 1}})
    yield "keys/metadata_extra", signed_tx(payload, metadata_extra={"timestamp": 1})
    yield "keys/metadata_empty", hex_bytes(canonical({"metadata": {}, "payload": payload}))
    yield "keys/metadata_list", hex_bytes(canonical({"metadata": [], "payload": payload}))
    yield "keys/metadata_missing", hex_bytes(canonical({"payload": payload}))
    yield "keys/metadata_wrong_key", hex_bytes(canonical({"metadata": {"sig": signature},
                                                           "payload": payload}))
    yield "keys/payload_missing", hex_bytes(canonical({"metadata": {"signature": signature}}))
    yield "keys/payload_list", hex_bytes(canonical({"metadata": {"signature": signature},
                                                     "payload": []}))
    yield "keys/payload_empty", hex_bytes(canonical({"metadata": {"signature": signature},
                                                      "payload": {}}))
    for key in payload:
        reduced = {k: v for k, v in payload.items() if k != key}
        yield f"keys/payload_without_{key}", signed_tx(reduced)
    yield "keys/payload_extra", signed_tx({**payload, "timestamp": 1})
    yield "keys/payload_extra_signed", signed_tx({**payload, "zzz": "x"})
    for key, values in {
        "sender": ["", None, 1, SENDER.upper(), SENDER[:-1], SENDER + "0", "g" * 64, []],
        "contract": ["", None, 1, "con_x", "1con", "_x", "con-x", "côn", "a" * 300],
        "function": ["", None, 1, "transfer", "_private", "f-x", "f x"],
        "chain_id": ["", None, 1, "other", CHAIN_ID.upper(), []],
        "nonce": [-1, 0, 1, True, False, None, "1", 1.5, 2**53 - 1, 2**53, 2**63, 2**64 - 1],
        "chi_supplied": [-1, 0, 1, True, False, None, "", "1", [], {}, 2**53 - 1, 2**53],
        "kwargs": [[], None, "x", 1, {}],
    }.items():
        for index, value in enumerate(values):
            label = json.dumps(value)[:20]
            yield f"field/{key}/{index}={label}", signed_tx({**payload, key: value})
    yield "field/signature_upper", hex_bytes(canonical({
        "metadata": {"signature": signature.upper()}, "payload": payload}))
    yield "field/signature_short", hex_bytes(canonical({
        "metadata": {"signature": signature[:-2]}, "payload": payload}))
    yield "field/signature_int", hex_bytes(canonical({"metadata": {"signature": 1},
                                                       "payload": payload}))
    for name in ("con_token", "con_", "con_Upper", "con_a.b", "token", "", None, 1, "con_" + "a" * 61):
        yield f"submission/name={json.dumps(name)}", signed_tx(base_payload(
            contract="submission", function="submit_contract",
            kwargs={"name": name, "code": "@export\ndef f():\n    return 1"}))
    yield "submission/no_name", signed_tx(base_payload(
        contract="submission", function="submit_contract", kwargs={"code": "x"}))
    for key in ("1a", "_a", "a-b", "", "é", "a b", "A_1"):
        yield f"kwargs_key/{key}", signed_tx(base_payload(kwargs={key: 1}))


def _tag_payloads() -> dict[str, list]:
    return {
        "__fixed__": ["0", "1", "-1", "0.5", "1.0", "-0", "0.0", "00.1", ".5", "1.", "1E+6",
                      "1e6", "NaN", "Infinity", " 1", "1 ", "+1", "0.1234567890123456789012345678901",
                      "0." + "1" * 30, "9" * 62, "9" * 63, 1, None, ["1"], "٣"],
        "__big_int__": [str(2**53), str(-(2**53)), str(2**53 - 1), "0", "1", "-0",
                        "0" + str(2**60), str(2**511), str(2**512), str(-(2**511)),
                        " " + str(2**60), str(2**60) + " ", "+" + str(2**60), 2**60,
                        str(2**64), "١" * 20, "9" * 156, "9" * 157],
        "__time__": [[2026, 1, 1], [2026, 1, 1, 0, 0, 0, 0], [2026, 2, 29], [2024, 2, 29],
                     [2026, 13, 1], [2026, 1, 1, 24, 0, 0, 0], [0, 1, 1], [10000, 1, 1],
                     [2026, 1], [2026, 1, 1, 0, 0, 0, 0, 0], [2026, 1, 1, 0, 0, 0, 999999],
                     [2026, 1, 1, 0, 0, 0, 1000000], [2026, "1", 1], [2026, 1.5, 1],
                     [2026, True, 1], [2**53, 1, 1], "2026-01-01", None, {}],
        "__delta__": [[0, 0], [1, 2], [-1, 0], [0, 86399], [0, 86400], [0, -1],
                      [999999999, 0], [1000000000, 0], [-999999999, 0], [1], [1, 2, 3],
                      ["1", 2], [True, 0], [2**53 - 1, 0], "12", None, {"0": 1}],
        "__bytes__": ["", "00", "00ff", "00FF", "0", "zz", "00 ff", " 00", "00\n", [0, 1],
                      1, None, "٠٠"],
        "__bytearray__": ["", "00ff", "ABCD", "0g", "00 ff", [1], None],
        "__set__": [[], [["int", 1]], [["int", 1], ["int", 2]], [["int", 2], ["int", 1]],
                    [["int", 1], ["int", 1]], [["str", "a"]], [["none"]], [["bool", True]],
                    [["bool", True], ["int", 1]], [["fixed", "1.5"]], [["fixed", "1.50"]],
                    [["tuple", [["int", 1], ["str", "x"]]]], [["frozenset", [["int", 1]]]],
                    [["bytes", "00"]], [["time", [2026, 1, 1, 0, 0, 0, 0]]],
                    [["delta", [1, 0]]], [["float", 1.5]], [["list", []]], [["int"]],
                    [["int", "1"]], [1], "x", None, {}, [["int", 2**60]],
                    [["int", {"__big_int__": str(2**60)}]]],
        "__frozenset__": [[], [["int", 1]], [["str", "b"], ["str", "a"]], [["str", "a"], ["str", "b"]],
                          [["tuple", []]], None],
    }


def _tags(rng: random.Random) -> Iterator[tuple[str, bytes]]:
    for tag, payloads in _tag_payloads().items():
        for index, payload in enumerate(payloads):
            yield _kwargs_case(f"tag/{tag}/{index}", {"v": {tag: payload}})
            yield _kwargs_case(f"tag/{tag}/{index}/nested", {"v": [{"x": {tag: payload}}]})
        yield _kwargs_case(f"tag/{tag}/two_keys", {"v": {tag: payloads[0], "x": 1}})
        yield _kwargs_case(f"tag/{tag}/two_tags", {"v": {tag: payloads[0], "__fixed__": "1"}})
    yield _kwargs_case("tag/unknown", {"v": {"__other__": 1}})
    yield _kwargs_case("tag/as_kwargs_key", {"__fixed__": "1"})
    yield _kwargs_case("tag/big_set", {"v": {"__set__": [["int", n] for n in range(5000)]}})
    yield _kwargs_case("tag/big_list", {"v": list(range(20000))})


def _scalar(rng: random.Random):
    choices = [
        lambda: rng.randint(-(2**53) + 1, 2**53 - 1),
        lambda: rng.choice([0, 1, -1, 2**53 - 1, -(2**53 - 1), 2**53, 2**63]),
        lambda: rng.choice([True, False, None]),
        lambda: "".join(rng.choice("abc é☃😀\\\"\n\t\x00/<>") for _ in range(rng.randint(0, 8))),
        lambda: {"__fixed__": rng.choice(["1.5", "0.001", "-2", "1.50", "1E+2", "abc"])},
        lambda: {"__big_int__": rng.choice([str(2**60), str(-(2**70)), "12", "0100"])},
        lambda: {"__time__": rng.choice([[2026, 1, 1], [2026, 2, 30], [2026, 1, 1, 1, 1, 1, 1]])},
        lambda: {"__delta__": rng.choice([[1, 2], [0, 90000], [1]])},
        lambda: {"__bytes__": rng.choice(["00ff", "0", "AB"])},
        lambda: {"__set__": rng.choice([[["int", 1]], [["int", 2], ["int", 1]], []])},
    ]
    return rng.choice(choices)()


def _value(rng: random.Random, depth: int = 0):
    if depth > 3 or rng.random() < 0.5:
        return _scalar(rng)
    if rng.random() < 0.5:
        return [_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {
        rng.choice(["a", "b", "key", "é", "", "__x"]): _value(rng, depth + 1)
        for _ in range(rng.randint(0, 4))
    }


def _generated(rng: random.Random, count: int) -> Iterator[tuple[str, bytes]]:
    for index in range(count):
        kwargs = {
            rng.choice(["amount", "to", "data", "items", "x1", "memo"]): _value(rng)
            for _ in range(rng.randint(0, 4))
        }
        yield _kwargs_case(f"generated/{index}", kwargs)


def _mutations(rng: random.Random, count: int) -> Iterator[tuple[str, bytes]]:
    payload = base_payload(kwargs={"amount": {"__fixed__": "1.5"}, "memo": "é", "to": SENDER})
    text = canonical({"metadata": {"signature": sign(payload)}, "payload": payload})
    alphabet = '{}[]",:0123456789-.eE+ abcfnrtu\\é_'
    for index in range(count):
        chars = list(text)
        for _ in range(rng.randint(1, 3)):
            at = rng.randrange(len(chars))
            operation = rng.random()
            if operation < 0.33:
                del chars[at]
            elif operation < 0.66:
                chars.insert(at, rng.choice(alphabet))
            else:
                chars[at] = rng.choice(alphabet)
        yield f"mutation/{index}", hex_bytes("".join(chars))


def _shared_vectors() -> Iterator[tuple[str, bytes]]:
    for name in ("transaction_wire.json", "transaction_numeric_v2.json"):
        path = WORKSPACE / "xian-contracting/tests/fixtures" / name
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            if case["payload"].get("chain_id") == CHAIN_ID:
                yield f"shared/{name}/{case['name']}", hex_bytes(case["transaction_json"])


def cases() -> list[tuple[str, bytes]]:
    rng = random.Random(SEED)
    generated = [
        *_handwritten(),
        *_key_sets(),
        *_tags(rng),
        *_generated(rng, 600),
        *_mutations(rng, 600),
        *_shared_vectors(),
    ]
    names = [name for name, _ in generated]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, duplicates
    return generated


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def outcome(admit, raw: bytes) -> list:
    """``["ok", digest of the decoded transaction]`` or ``["error", text]``."""
    try:
        value = admit(raw)
    except Exception as exc:  # noqa: BLE001 - the error text is the outcome
        return ["error", str(exc)]
    return ["ok", digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())]


def node_admission(raw: bytes):
    from xian.utils.tx import decode_and_validate_transaction_static_bytes

    return decode_and_validate_transaction_static_bytes(raw, chain_id=CHAIN_ID)


def run(admit=node_admission) -> dict[str, dict]:
    return {
        name: {"raw": digest(raw), "outcome": outcome(admit, raw)} for name, raw in cases()
    }


def load_fixture() -> dict[str, dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if argv != ["--write"]:
        print(__doc__)
        return 2
    FIXTURE.write_text(
        json.dumps(run(), indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
