"""The state root built from committed storage equals the Python reference root.

``StateRootCache.from_driver`` reads LMDB in Rust; ``state_root_reference``
hashes the same stored bytes with a plain Python treap. Every corpus here must
give the same root and leaf count, or raise the same error. A leaf commits to
the stored bytes, so values are never decoded to build the root.
"""

import random

import pytest
from contracting import constants as contracting_constants
from contracting.runtime_features import RUNTIME_FEATURE_STATE_PREFIX
from contracting.storage.driver import Driver
from state_root_reference import EMPTY, driver_root, root_of_items
from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime, Timedelta

from xian.execution_policy import EXECUTION_POLICY_KEY
from xian.fee_policy import FEE_POLICY_KEY
from xian.state_root import StateRootCache, stored_bytes

NONCE_PREFIX = f"__n{contracting_constants.INDEX_SEPARATOR}"

EVERY_VALUE_TYPE = {
    "con_t.int:small": 7,
    "con_t.int:negative": -5,
    "con_t.int:zero": 0,
    "con_t.int:safe_max": 2**53 - 1,
    "con_t.int:safe_min": -(2**53 - 1),
    "con_t.int:big": 2**53,
    "con_t.int:big_negative": -(2**200),
    "con_t.bool:true": True,
    "con_t.bool:false": False,
    "con_t.str:plain": "hello",
    "con_t.str:empty": "",
    "con_t.str:escapes": 'q"b\\s/\n\r\t\x08\x0c\x00\x1f\x7f',
    "con_t.str:unicode": "é \U0001f600中￿",
    "con_t.dec:fraction": ContractingDecimal("1.5"),
    "con_t.dec:tiny": ContractingDecimal("-0.000000000000000001"),
    "con_t.dec:wide": ContractingDecimal("123456789012345678901234567890.123"),
    "con_t.dec:zero": ContractingDecimal("0"),
    "con_t.dec:integral": ContractingDecimal("100"),
    "con_t.time:whole": Datetime(2024, 1, 2, 3, 4, 5),
    "con_t.time:micro": Datetime(2024, 12, 31, 23, 59, 59, 6),
    "con_t.time:first": Datetime(1, 1, 1),
    "con_t.bytes:utf8": b"ok",
    "con_t.bytes:empty": b"",
    "con_t.bytes:unicode": "é".encode(),
    "con_t.bytes:binary": b"\xff'",
    "con_t.bytes:quotes": b"\xfe'\"",
    "con_t.set:mixed": ContractingSet(
        {(1, b"x"), True, None, 2**80, -3, "é", ContractingFrozenSet({"a"})}
    ),
    "con_t.set:empty": ContractingSet(),
    "con_t.frozenset:bytes": ContractingFrozenSet({b"q", b"\xff"}),
    "con_t.list:nested": [1, "a", [2, [3]], {"z": 1, "a": 2}],
    "con_t.list:empty": [],
    "con_t.tuple": (1, 2),
    "con_t.dict:nested": {
        "b": 1,
        "a": {"d": [Datetime(2020, 2, 29), ContractingDecimal("2.5"), b"x"], "c": None},
        "é": 1,
        "Z": 2,
        "": [],
    },
    "con_t.dict:empty": {},
    "con_t.dict:set_value": {"s": ContractingSet({"b", "a"})},
    "con_t.delta:day": Timedelta(days=1, seconds=5),
    "con_t.delta:negative": Timedelta(days=-3, seconds=-5),
    "con_t.delta:weeks": Timedelta(weeks=1),
    "con_t.delta:zero": Timedelta(),
    "con_t.delta:nested": {"d": [Timedelta(days=2)], "a": Timedelta(seconds=1)},
    "con_t.bytearray:text": bytearray(b"ab"),
    "con_t.bytearray:binary": bytearray(b"\xff\x00"),
    "con_t.bytearray:empty": bytearray(),
    "con_t.bytearray:nested": [bytearray(b"x"), {"b": bytearray(b"\xfe")}],
}


def stored(value) -> bytes:
    return encode(value).encode()


def committed(tmp_path, values=(), raw=()):
    """A V2 driver whose LMDB holds ``values`` (encoded) and ``raw`` stored bytes."""
    driver = Driver(storage_home=tmp_path)
    environment = driver.committed_storage()[0]
    changes = [(key.encode(), stored(value)) for key, value in dict(values).items()]
    changes += [(key, value) for key, value in dict(raw).items()]
    environment.write(changes)
    return driver


def assert_reference_root(driver):
    expected_root, expected_leaves = driver_root(driver)
    cache = StateRootCache.from_driver(driver)
    assert cache.root_hash.hex() == expected_root.hex()
    assert cache.leaf_count == expected_leaves
    return cache


def test_empty_state_has_the_empty_root(tmp_path):
    cache = assert_reference_root(committed(tmp_path))
    assert cache.root_hash == EMPTY
    assert cache.leaf_count == 0


def test_every_value_type_hashes_like_python(tmp_path):
    assert_reference_root(committed(tmp_path, EVERY_VALUE_TYPE))


@pytest.mark.parametrize("key, value", sorted(EVERY_VALUE_TYPE.items()))
def test_each_value_type_alone_hashes_like_python(tmp_path, key, value):
    assert_reference_root(committed(tmp_path, {key: value}))


def test_stored_text_is_hashed_as_stored_whatever_its_spelling(tmp_path):
    raw = {
        b"con_t.str:lone_surrogate": b'"\\ud800"',
        b"con_t.dict:surrogate_key": b'{"\\udfff":1,"a":"\\ud83d"}',
        b"con_t.str:spaced": b' [ 1 , {"b" : 2 , "a" : 1} ] ',
        b"con_t.dict:duplicate": b'{"a":1,"b":2,"a":3}',
        b"con_t.str:escaped_slash": b'"a\\/b"',
    }
    assert_reference_root(committed(tmp_path, EVERY_VALUE_TYPE, raw))


def test_stored_null_values_are_tombstones(tmp_path):
    raw = {b"con_t.gone": b"null", b"__n:alice:": b"null", b"con_t.kept": b"1"}
    cache = assert_reference_root(committed(tmp_path, raw=raw))
    assert cache.leaf_count == 1


def test_pending_writes_and_deletions_overlay_committed_state(tmp_path):
    driver = committed(tmp_path, EVERY_VALUE_TYPE)
    driver.set("con_t.int:small", None)
    driver.set("con_t.dict:nested", None)
    driver.set("con_t.int:zero", 99)
    driver.set("con_t.new", {"z": Datetime(2021, 5, 6), "a": b"\xff"})
    driver.set(f"{NONCE_PREFIX}bob:", 3)
    driver.set("__local.cache", 4)
    assert_reference_root(driver)


def test_only_consensus_keys_are_hashed(tmp_path):
    values = {
        f"{NONCE_PREFIX}alice:": 7,
        EXECUTION_POLICY_KEY: {"revision": 13},
        FEE_POLICY_KEY: {"max_tx_chi": 200000},
        f"{RUNTIME_FEATURE_STATE_PREFIX}zk": True,
        "__local.cache": 1,
        "__latest_block.height": 9,
        "__runtime_featuresx": 1,
        "_single.underscore": 1,
        "con_t.a": 1,
    }
    cache = assert_reference_root(committed(tmp_path, values))
    assert cache.leaf_count == 6


def test_long_and_unicode_keys_order_like_python(tmp_path):
    keys = [
        "con_t.k:" + "x" * 503,
        "con_t.k:" + "é" * 251,
        "con_t.k:a",
        "con_t.k:a\x7f",
        "con_t.k:aé",
        "con_t.k:a￿",
        "con_t.k:a\U0001f600",
        "con_t.k:b",
        "con_t.k:",
    ]
    assert_reference_root(committed(tmp_path, {key: index for index, key in enumerate(keys)}))


def test_node_local_timedeltas_and_bytearrays_are_skipped(tmp_path):
    values = {"__local.delta": Timedelta(days=1), "__local.raw": bytearray(b"x"), "con_t.a": 1}
    assert_reference_root(committed(tmp_path, values))


@pytest.mark.parametrize("key", [b"con_t.corrupt", b"__local.corrupt"])
def test_values_are_hashed_without_decoding_them(tmp_path, key):
    assert_reference_root(committed(tmp_path, {"con_t.ok": 1}, {key: b'{"a":1.5}'}))


def test_a_stored_key_that_is_not_text_is_refused(tmp_path):
    driver = committed(tmp_path, {"con_t.ok": 1}, {b"con_t.\xff": b"1"})
    with pytest.raises(ValueError):
        driver_root(driver)
    with pytest.raises(ValueError, match="not UTF-8"):
        StateRootCache.from_driver(driver)


def test_prepared_leaves_are_the_bytes_commit_writes(tmp_path):
    driver = committed(tmp_path, {"con_t.kept": 1, "con_t.gone": 2})
    cache = StateRootCache.from_driver(driver)
    for key, value in EVERY_VALUE_TYPE.items():
        driver.set(key, value)
    driver.set("con_t.gone", None)
    pending = driver.pending_stored_items()
    prepared_root = cache.prepare(pending)
    driver.commit()
    environment = driver.committed_storage()[0]
    assert all(environment.get(key.encode()) == stored for key, stored in pending)
    assert StateRootCache.from_driver(driver).root_hash == prepared_root
    assert driver_root(driver)[0] == prepared_root


def random_value(rng: random.Random, depth: int = 0):
    scalars = [
        lambda: rng.randrange(-(2**60), 2**60),
        lambda: rng.randrange(-100, 100),
        lambda: rng.choice([True, False]),
        lambda: "".join(rng.choice('ab"\\\né \U0001f600 ') for _ in range(rng.randrange(6))),
        lambda: ContractingDecimal(f"{rng.randrange(-(10**12), 10**12)}.{rng.randrange(10**6)}"),
        lambda: Datetime(
            rng.randrange(1, 9999),
            rng.randrange(1, 13),
            rng.randrange(1, 29),
            rng.randrange(24),
            rng.randrange(60),
            rng.randrange(60),
            rng.choice([0, rng.randrange(10**6)]),
        ),
        lambda: rng.randbytes(rng.randrange(4)),
        lambda: bytearray(rng.randbytes(rng.randrange(4))),
        lambda: Timedelta(days=rng.randrange(-400, 400), seconds=rng.randrange(-86400, 86400)),
        lambda: ContractingSet({rng.randrange(50) for _ in range(rng.randrange(4))}),
        lambda: ContractingFrozenSet({f"m{rng.randrange(9)}" for _ in range(rng.randrange(3))}),
    ]
    roll = rng.random()
    if depth < 3 and roll < 0.15:
        return [random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    if depth < 3 and roll < 0.3:
        return {
            "".join(rng.choice("abcZé_") for _ in range(rng.randrange(1, 4))): random_value(
                rng, depth + 1
            )
            for _ in range(rng.randrange(4))
        }
    if depth > 0 and roll < 0.33:
        return None
    return rng.choice(scalars)()


def random_key(rng: random.Random, index: int) -> str:
    prefix = rng.choice(["con_r.v:", "con_r.h:", NONCE_PREFIX, "__local.", "con_é.k:"])
    return f"{prefix}{index:05d}{'x' * rng.randrange(0, 400, 50)}"


@pytest.mark.parametrize("seed", [20261113, 7, 42])
def test_a_random_corpus_hashes_like_python(tmp_path, seed):
    rng = random.Random(seed)
    values = {random_key(rng, index): random_value(rng) for index in range(1500)}
    raw = {f"con_r.null:{index}".encode(): b"null" for index in range(20)}
    driver = committed(tmp_path, values, raw)
    for index in range(0, 1500, 7):
        key = random_key(random.Random(index), index)
        driver.set(key, None if index % 2 else random_value(rng))
    assert_reference_root(driver)


def test_blocks_after_startup_keep_the_reference_root(tmp_path):
    rng = random.Random(5)
    values = {f"con_b.v:{index}": random_value(rng) for index in range(400)}
    driver = committed(tmp_path, values)
    cache = StateRootCache.from_driver(driver)
    start_root = cache.root_hash
    writes = [(f"con_b.v:{index}", None if index % 3 == 0 else index) for index in range(0, 600, 4)]
    merged = dict(values)
    for key, value in writes:
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    stored_writes = [(key, stored_bytes(value)) for key, value in writes]
    assert cache.prepare(stored_writes) == root_of_items(merged.items())
    cache.rollback()
    assert cache.root_hash == start_root
    cache.prepare(stored_writes)
    cache.commit()
    assert cache.root_hash == root_of_items(merged.items())


def test_v1_state_is_left_to_its_pinned_runtime(tmp_path):
    driver = Driver(storage_home=tmp_path, vm_profile="xian_vm_v1")
    driver.set("con_t.a", 1)
    driver.commit()
    with pytest.raises(ValueError, match="V1 state"):
        StateRootCache.from_driver(driver)
