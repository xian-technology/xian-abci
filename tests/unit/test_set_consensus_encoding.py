import pytest
from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.encoding import decode

from xian.methods.query import _encode_query_value
from xian.state_root import merkle_root_from_items
from xian.utils.encoding import encode_abci_json


@pytest.mark.parametrize("kind", [ContractingSet, ContractingFrozenSet])
def test_wrapper_set_queries_receipts_and_state_roots_use_lossless_codec(kind):
    value = kind([(1, "a"), ContractingFrozenSet([b"x"])])
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
