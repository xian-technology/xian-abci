"""Values a contract stores or returns cross the execution boundary in their
wire form; state a value has no stored form for fails the transaction."""

import pytest
from contracting.local import ContractingClient
from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.time import Timedelta

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.encoding import encode_abci_json
from xian.utils.tx import transaction_bytes


STORED = {"bytearray(b'abc')": bytearray(b"abc"), "datetime.timedelta(days=1)": Timedelta(days=1)}


@pytest.mark.parametrize(
    "expression",
    [
        "{1: 2}",
        "set([1, 2])",
        "frozenset([1, 2])",
        "bytearray(b'abc')",
        "datetime.timedelta(days=1)",
    ],
)
@pytest.mark.parametrize("store", [False, True])
def test_processor_stores_and_returns_wire_values_but_rejects_unencodable_state(
    tmp_path,
    expression,
    store,
):
    pytest.importorskip("xian_vm_core")
    source = "valid = Variable()\ninvalid = Variable()\n\n@export\ndef probe():\n"
    source += "    valid.set(7)\n"
    source += f"    invalid.set({expression})\n" if store else f"    return {expression}\n"
    with ContractingClient(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as client:
        client.submit(source, name="con_boundary", owner=None)
        client.raw_driver.set("currency.balances:alice", 100_000)
        client.raw_driver.set("chi_cost.S:value", 1)
        client.raw_driver.commit()
        processor = TxProcessor(client)
        tx = {
            "payload": {
                "sender": "alice",
                "contract": "con_boundary",
                "function": "probe",
                "kwargs": {},
                "nonce": 0,
                "chi_supplied": 2_000,
                "chain_id": "test",
            },
            "metadata": {"signature": "test"},
            "b_meta": {"nanos": 0, "hash": "test", "height": 1, "chain_id": "test"},
        }
        [response] = processor.process_block(
            [tx], [transaction_bytes(tx)], fee_policy=TxFeePolicy.paid_metered()
        )
        output = response["tx_result"]
        assert output is not None
        wrapper = expression.startswith(("set(", "frozenset("))
        # Sets, bytearrays and timedeltas have a wire form; a dict with an
        # int key has none and can only be returned (as text).
        failed = expression == "{1: 2}"
        assert output["status"] == int(failed)
        assert output["chi_used"] > 0
        assert client.raw_driver.get("currency.balances:alice") < 100_000
        assert client.raw_driver.get("con_boundary.valid") == (None if failed else 7)
        stored = client.raw_driver.get("con_boundary.invalid")
        if store and wrapper:
            assert sorted(stored) == [1, 2]
            assert isinstance(
                stored,
                ContractingFrozenSet if expression.startswith("frozenset") else ContractingSet,
            )
        elif store and not failed:
            assert stored == STORED[expression]
        else:
            assert stored is None
        if failed:
            assert isinstance(output["result"], str)
        elif store:
            assert output["result"] is None
        else:
            assert isinstance(output["result"], dict)
        encode_abci_json(output)
