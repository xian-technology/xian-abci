from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from contracting.local import ContractingClient
from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime, Timedelta

from xian.execution_engine import build_vm_runtime, execute_vm_contract
from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.encoding import encode_abci_json


def execute_output(**fields):
    native = SimpleNamespace(
        **{
            "status_code": 0,
            "result": "ok",
            "writes": {},
            "events": [],
            "chi_used": 7,
            "contract_costs": {"con_demo": 7},
            **fields,
        }
    )
    with patch("xian.execution_engine._load_vm_runtime_bindings") as bindings:
        bindings.return_value.execute_contract.return_value = native
        return execute_vm_contract(
            SimpleNamespace(),
            SimpleNamespace(),
            sender="alice",
            contract_name="con_demo",
            function_name="probe",
            kwargs={},
            environment={},
        )


@pytest.mark.parametrize(
    "value",
    [
        {1: 2},
        [{"nested": {5: 1}}],
        ContractingSet([1, 2]),
        ContractingFrozenSet([1, 2]),
        bytearray(b"abc"),
        Timedelta(days=1),
    ],
)
@pytest.mark.parametrize("field", ["writes", "events"])
def test_invalid_effect_discards_all_effects_and_preserves_consumed_chi(value, field):
    writes = {"con_demo.valid": 7}
    events = [{"event": "Valid", "data": {"value": 7}}]
    if field == "writes":
        writes["con_demo.invalid"] = value
    else:
        events.append({"event": "Invalid", "data": {"value": value}})
    output = execute_output(writes=writes, events=events)
    assert output.status_code == 1
    assert isinstance(output.result, ValueError)
    assert output.writes == {}
    assert output.events == []
    assert output.chi_used == 7
    assert output.contract_costs == {"con_demo": 7}


@pytest.mark.parametrize("key", [1, "con_demo.\ud800"])
def test_state_keys_must_be_utf8_strings(key):
    output = execute_output(writes={key: 1})
    assert output.status_code == 1
    assert output.writes == {}


def test_cyclic_effect_is_a_transaction_failure():
    value = []
    value.append(value)
    output = execute_output(writes={"con_demo.value": value})
    assert output.status_code == 1
    assert output.writes == {}


def test_supported_effects_and_return_only_dict_keep_their_original_values():
    value = {
        "values": [None, True, 2**128, 1.5, "text", b"\xff", (1, 2)],
        "decimal": Decimal("1.25"),
        "fixed": ContractingDecimal("2.5"),
        "time": Datetime(2026, 10, 30),
        "datetime": datetime(2026, 10, 30),
    }
    output = execute_output(
        writes={"con_demo.value": value}, events=[{"data": value}], result={1: 2}
    )
    assert output.status_code == 0
    assert output.writes["con_demo.value"] is value
    assert output.events == [{"data": value}]
    assert output.result == {1: 2}


def test_native_failure_keeps_its_original_error_and_cost():
    error = ValueError("native failure")
    output = execute_output(status_code=1, result=error)
    assert output.status_code == 1
    assert output.result is error
    assert output.chi_used == 7


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
def test_processor_preserves_return_values_but_rejects_unencodable_state(
    tmp_path,
    expression,
    store,
):
    pytest.importorskip("xian_vm_core")
    source = "valid = Variable()\ninvalid = Variable()\n\n@export\ndef probe():\n"
    source += "    valid.set(7)\n"
    source += f"    invalid.set({expression})\n" if store else f"    return {expression}\n"
    with ContractingClient(storage_home=tmp_path / "state") as client:
        client.submit(source, name="con_boundary", owner=None)
        client.raw_driver.set("currency.balances:alice", 100_000)
        client.raw_driver.commit()
        processor = TxProcessor(client, execution_runtime=build_vm_runtime())
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
        output = processor.process_tx(tx, fee_policy=TxFeePolicy.paid_metered())["tx_result"]
        assert output is not None
        assert output["status"] == int(store)
        assert output["chi_used"] > 0
        assert client.raw_driver.get("currency.balances:alice") < 100_000
        assert client.raw_driver.get("con_boundary.valid") == (None if store else 7)
        assert client.raw_driver.get("con_boundary.invalid") is None
        assert isinstance(output["result"], str)
        encode_abci_json(output)
