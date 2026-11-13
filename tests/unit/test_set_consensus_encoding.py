import pytest
from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.encoding import decode

from xian.execution_engine import VmExecutionResult, _validated_vm_result
from xian.methods.query import _encode_query_value
from xian.state_root import merkle_root_from_items
from xian.utils.encoding import encode_abci_json


@pytest.mark.parametrize("kind", [ContractingSet, ContractingFrozenSet])
def test_wrapper_set_effects_and_queries_use_lossless_codec(kind):
    value = kind([(1, "a"), ContractingFrozenSet([b"x"])])
    output = _validated_vm_result(VmExecutionResult(0, None, {"con_demo.value": value}, [], 7))
    assert output.status_code == 0, output.result
    assert output.writes["con_demo.value"] is value
    encoded, info = _encode_query_value(value)
    assert info == "dict"
    restored = decode(encoded)
    assert type(restored) is kind
    assert restored == value
    assert isinstance(list(restored)[0], tuple)
    nested = decode(encode_abci_json({"nested": [value]}))
    assert type(nested["nested"][0]) is kind
    assert nested["nested"][0] == value
    assert merkle_root_from_items([("value", restored)]) == merkle_root_from_items(
        [("value", value)]
    )


def test_non_set_consensus_bytes_are_unchanged():
    assert (
        encode_abci_json({"b": b"abc", "a": [1, True, (2, "x")]})
        == b'{"a":[1,true,[2,"x"]],"b":"abc"}'
    )


@pytest.mark.parametrize("tag", ["__set__", "__frozenset__", "__fixed__", "__time__"])
def test_reserved_tag_dictionary_effects_fail_before_application(tag):
    output = _validated_vm_result(
        VmExecutionResult(0, None, {"con_demo.value": {"nested": [{tag: [], "other": 7}]}}, [], 9)
    )
    assert output.status_code == 1
    assert output.writes == {} and output.events == []
    assert output.chi_used == 9


def test_protected_execution_policy_cannot_be_written_by_contract_output():
    output = _validated_vm_result(VmExecutionResult(0, None, {"__execution_policy": {}}, [], 9))
    assert output.status_code == 1 and output.writes == {}


def test_a_key_lmdb_cannot_store_fails_before_application():
    # LMDB stores keys of at most 511 bytes; a longer one would only fail at Commit.
    largest = "con_demo.value:" + "é" * 248
    assert len(largest.encode()) == 511
    assert _validated_vm_result(VmExecutionResult(0, None, {largest: 1}, [], 9)).status_code == 0

    output = _validated_vm_result(VmExecutionResult(0, None, {largest + "x": 1}, [], 9))
    assert output.status_code == 1 and output.writes == {} and output.events == []
    assert output.chi_used == 9
