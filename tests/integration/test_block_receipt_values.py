"""Receipt bytes of V2 values that cross the VM boundary of a block.

A set keeps its V2 members (True and 1 are different members) in the
result, the state writes and the events of a receipt, as the stored bytes
keep them; an exception's list and dict arguments reach the receipt whole.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from contracting.local import ContractingClient
from xian_runtime_types.decimal import ContractingDecimal

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.encoding import encode_abci_json
from xian.utils.tx import transaction_bytes

pytest.importorskip("xian_vm_core")

ROOT = Path(__file__).resolve().parents[3]
ALICE = "a" * 64
VALUES = r"""kept = Variable()
Seen = LogEvent("Seen", {"members": set})

@export
def members():
    pairs = frozenset([(True, 'x'), (1, 'x')])
    members = set([True, 1, pairs])
    kept.set(members)
    Seen({"members": set([True, 1])})
    return members

@export
def fail():
    raise Exception([1, {'a': (2, 3)}], 'b', None, True)

@export
def receipt_types():
    return {'z': [datetime.datetime(2026, 1, 2, 3, 4, 5, 6),
                  datetime.timedelta(seconds=-90001), bytearray([255]),
                  b"\xff'", b"hello", decimal('0.125'), 9007199254740993],
            'a': frozenset([True, 1])}

@export
def fail_mixed():
    raise Exception("can't", b"\xff'", bytearray([255]), set(["can't"]),
                    {"key": datetime.timedelta(seconds=-1)})

@export
def fail_range():
    raise Exception(range(3))
"""
MEMBERS = {
    "__set__": {
        "codec": "xian-set-1",
        "items": [
            ["bool", True],
            ["int", "1"],
            [
                "frozenset",
                [
                    ["tuple", [["bool", True], ["str", "x"]]],
                    ["tuple", [["int", "1"], ["str", "x"]]],
                ],
            ],
        ],
    }
}


@pytest.fixture
def chain(tmp_path):
    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=tmp_path)
    driver = client.raw_driver
    driver.set_contract_from_source(
        "currency", (ROOT / "xian-configs/contracts/currency.s.py").read_text()
    )
    driver.set_contract_from_source("con_values", VALUES)
    driver.set(f"currency.balances:{ALICE}", ContractingDecimal("10") ** 18)
    driver.set("chi_cost.S:value", 20)
    driver.commit()
    yield client, TxProcessor(client=client)
    client.close()


def _receipt(chain, function):
    return json.loads(_receipt_bytes(chain, function))


def _receipt_bytes(chain, function, *, receipt_details=None):
    client, processor = chain
    tx = {
        "payload": {
            "chain_id": "xian-local",
            "chi_supplied": 5_000,
            "contract": "con_values",
            "function": function,
            "kwargs": {},
            "nonce": 0,
            "sender": ALICE,
        },
        "metadata": {"signature": "f" * 128},
        "b_meta": {
            "nanos": 1_790_000_000_000_000_000,
            "height": 1,
            "hash": "0" * 64,
            "chain_id": "xian-local",
        },
    }
    (response,) = processor.process_block(
        [tx],
        [transaction_bytes(tx)],
        fee_policy=TxFeePolicy.paid_metered(),
        receipt_details=receipt_details,
    )
    if "receipt" not in response:
        assert receipt_details is None or not processor.native_receipts
        return encode_abci_json(response["tx_result"])
    if receipt_details:
        assert response["receipt"] == encode_abci_json(response["tx_result"])
    return response["receipt"]


def test_a_set_keeps_true_and_1_apart_in_result_writes_and_events(chain):
    receipt = _receipt(chain, "members")

    assert receipt["status"] == 0, receipt["result"]
    assert receipt["result"] == MEMBERS
    state = {write["key"]: write["value"] for write in receipt["state"]}
    assert state["con_values.kept"] == MEMBERS
    [event] = receipt["events"]
    assert event["data"]["members"] == {
        "__set__": {"codec": "xian-set-1", "items": [["bool", True], ["int", "1"]]}
    }
    # The receipt shows the value the block stored.
    client, _ = chain
    client.raw_driver.commit()
    assert json.loads(encode_abci_json(client.raw_driver.get("con_values.kept"))) == MEMBERS


def test_an_exception_keeps_its_list_and_dict_arguments(chain):
    receipt = _receipt(chain, "fail")

    assert receipt["status"] == 1
    assert receipt["result"] == "Exception([1, {'a': (2, 3)}], 'b', None, True)"


def test_remaining_tagged_and_text_values_match_python(chain):
    receipt = _receipt(chain, "receipt_types")

    assert receipt["status"] == 0, receipt["result"]
    assert receipt["result"]["z"] == [
        "2026-01-02 03:04:05.000006",
        {"__delta__": [-2, 82799]},
        {"__bytearray__": "ff"},
        'b"\\xff\'"',
        "hello",
        {"__fixed__": "0.125"},
        {"__big_int__": "9007199254740993"},
    ]


@pytest.mark.parametrize("function", ["fail_mixed", "fail_range"])
def test_exception_wire_text_matches_python_for_binary_values_and_local_objects(chain, function):
    assert _receipt(chain, function)["status"] == 1


@pytest.mark.parametrize(
    "function", ["members", "receipt_types", "fail", "fail_mixed", "fail_range"]
)
def test_native_receipt_projection_modes_match_python(chain, function):
    client, _ = chain
    expected = _receipt_bytes(chain, function)
    processor = TxProcessor(client)
    assert processor.native_receipts
    for details in (True, False):
        client.raw_driver.rollback()
        assert _receipt_bytes((client, processor), function, receipt_details=details) == expected
