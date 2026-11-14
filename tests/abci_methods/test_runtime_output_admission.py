import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from nacl.signing import SigningKey
from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.time import Timedelta

from cometbft.abci.v1beta2.types_pb2 import ResponseProcessProposal
from cometbft.abci.v1beta3.types_pb2 import (
    RequestFinalizeBlock,
    RequestPrepareProposal,
    RequestProcessProposal,
)
from xian.constants import Constants
from xian.fee_policy import TxFeePolicy
from xian.state_root import StateRootCache
from xian.utils.encoding import canonical_json_text, encode_transaction_bytes
from xian.xian_abci import Xian

SIGNING_KEY = SigningKey(bytes(range(32)))
SENDER = SIGNING_KEY.verify_key.encode().hex()


@asynccontextmanager
async def isolated_app(tmp_path, source):
    pytest.importorskip("xian_vm_core")
    home = tmp_path / "cometbft"
    shutil.copytree(Path(__file__).parent / "fixtures/.cometbft-fixture", home)

    class TestConstants(Constants):
        COMETBFT_HOME = home
        COMETBFT_CONFIG = home / "config/config.toml"
        XIAN_CONFIG = home / "config/xian.toml"
        COMETBFT_GENESIS = home / "config/genesis.json"
        STORAGE_HOME = home / "xian"

    app = await Xian.create(constants=TestConstants)
    try:
        app.client.submit(source, name="con_boundary", owner=None)
        app.client.raw_driver.set(f"currency.balances:{SENDER}", 100_000)
        app.client.raw_driver.set("chi_cost.S:value", 20)
        app.client.raw_driver.commit()
        app.state_root_cache = StateRootCache.from_driver(app.client.raw_driver)
        yield app
    finally:
        await app.close()
        app.client.close()


def signed_tx(app, *, function, nonce, chi=2_000):
    payload = {
        "sender": SENDER,
        "contract": "con_boundary",
        "function": function,
        "kwargs": {},
        "nonce": nonce,
        "chi_supplied": chi,
        "chain_id": app.chain_id,
    }
    signature = SIGNING_KEY.sign(canonical_json_text(payload).encode()).signature.hex()
    return encode_transaction_bytes(
        canonical_json_text(
            {
                "payload": payload,
                "metadata": {"signature": signature},
            }
        )
    )


@pytest.mark.parametrize("expression", ["{1: 2}"])
def test_finalize_rejects_invalid_state_and_commits_following_transaction(tmp_path, expression):
    source = (
        "value = Variable()\nvalid = Variable()\n\n"
        "@export\ndef invalid():\n    valid.set(99)\n"
        f"    value.set({expression})\n\n"
        "@export\ndef good():\n    valid.set(7)\n    return {'value': 7}\n"
    )

    async def run():
        async with isolated_app(tmp_path, source) as app:
            txs = [
                signed_tx(app, function="invalid", nonce=0),
                signed_tx(app, function="good", nonce=1),
            ]
            result = await app.finalize_block(
                RequestFinalizeBlock(height=1, hash=b"1" * 32, txs=txs)
            )
            assert [tx.code for tx in result.tx_results] == [1, 0]
            failed = json.loads(result.tx_results[0].data)
            assert failed["chi_used"] > 0
            assert failed["events"] == []
            assert not any(write["key"].startswith("con_boundary.") for write in failed["state"])
            await app.commit()
            assert app.client.raw_driver.get("con_boundary.value") is None
            assert app.client.raw_driver.get("con_boundary.valid") == 7
            assert app.state_root_cache.root_hash == result.app_hash

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["set", "frozenset"])
def test_finalize_persists_wrapper_sets_and_reads_typed_members_next_block(tmp_path, kind):
    source = (
        "value = Variable()\n"
        "@export\ndef store():\n"
        f"    value.set({kind}([(1, 2), frozenset([3])]))\n"
        "@export\ndef read():\n"
        "    stored = value.get()\n"
        "    return [(1, 2) in stored, frozenset([3]) in stored]\n"
    )

    async def run():
        async with isolated_app(tmp_path, source) as app:
            first = await app.finalize_block(
                RequestFinalizeBlock(
                    height=1,
                    hash=b"1" * 32,
                    txs=[signed_tx(app, function="store", nonce=0)],
                )
            )
            assert first.tx_results[0].code == 0
            await app.commit()
            app.client.raw_driver.flush_cache()
            stored = app.client.raw_driver.get("con_boundary.value")
            assert type(stored) is (ContractingSet if kind == "set" else ContractingFrozenSet)
            assert (1, 2) in stored and ContractingFrozenSet([3]) in stored
            assert StateRootCache.from_driver(app.client.raw_driver).root_hash == first.app_hash
            second = await app.finalize_block(
                RequestFinalizeBlock(
                    height=2,
                    hash=b"2" * 32,
                    txs=[signed_tx(app, function="read", nonce=1)],
                )
            )
            assert second.tx_results[0].code == 0
            assert json.loads(second.tx_results[0].data)["result"] == [True, True]
            await app.commit()

    asyncio.run(run())


@pytest.mark.parametrize(
    "expression, expected, wire",
    [
        (
            "datetime.timedelta(days=-999999999)",
            Timedelta(days=-999_999_999),
            {"__delta__": [-999_999_999, 0]},
        ),
        ("bytearray(b'a\\xff')", bytearray(b"a\xff"), {"__bytearray__": "61ff"}),
    ],
)
def test_finalize_persists_timedelta_and_bytearray_and_reads_them_next_block(
    tmp_path, expression, expected, wire
):
    source = (
        "value = Variable()\n"
        f"@export\ndef store():\n    value.set({expression})\n"
        "@export\ndef read():\n    return value.get()\n"
    )

    async def run():
        async with isolated_app(tmp_path, source) as app:
            first = await app.finalize_block(
                RequestFinalizeBlock(
                    height=1,
                    hash=b"1" * 32,
                    txs=[signed_tx(app, function="store", nonce=0)],
                )
            )
            assert first.tx_results[0].code == 0
            state = json.loads(first.tx_results[0].data)["state"]
            assert {"key": "con_boundary.value", "value": wire} in state
            await app.commit()
            app.client.raw_driver.flush_cache()
            stored = app.client.raw_driver.get("con_boundary.value")
            assert type(stored) is type(expected) and stored == expected
            assert StateRootCache.from_driver(app.client.raw_driver).root_hash == first.app_hash
            second = await app.finalize_block(
                RequestFinalizeBlock(
                    height=2,
                    hash=b"2" * 32,
                    txs=[signed_tx(app, function="read", nonce=1)],
                )
            )
            assert second.tx_results[0].code == 0
            assert json.loads(second.tx_results[0].data)["result"] == wire
            await app.commit()

    asyncio.run(run())


@pytest.mark.parametrize("policy", [TxFeePolicy.paid_metered(), TxFeePolicy.free_metered()])
def test_zero_chi_is_rejected_by_all_transaction_entrypoints(tmp_path, policy):
    async def run():
        async with isolated_app(tmp_path, "@export\ndef good():\n    return 1\n") as app:
            app.tx_fee_policy = policy
            zero = signed_tx(app, function="good", nonce=0, chi=0)
            valid = signed_tx(app, function="good", nonce=0)
            check = await app.check_tx(zero)
            assert check.code == 1
            assert "chi_supplied" in check.log
            prepared = await app.prepare_proposal(
                RequestPrepareProposal(max_tx_bytes=1_000_000, txs=[zero, valid])
            )
            assert list(prepared.txs) == [valid]
            proposal = await app.process_proposal(RequestProcessProposal(txs=[zero]))
            assert proposal.status == ResponseProcessProposal.REJECT
            result = await app.finalize_block(
                RequestFinalizeBlock(height=1, hash=b"1" * 32, txs=[zero, valid])
            )
            assert [tx.code for tx in result.tx_results] == [1, 0]
            assert "chi_supplied" in json.loads(result.tx_results[0].data)["error"]

    asyncio.run(run())


# LMDB refuses keys over 511 bytes. Each write below forms a 512-byte storage
# key ("con_boundary.data:" is 18 bytes); the last one forms exactly 511.
# V2 admission keeps every variable key within the limit, so only hash keys can
# exceed it at runtime.
_TARGET = "t" * 98
STORAGE_KEY_SOURCE = (
    f"data = Hash(default_value=0)\n{_TARGET} = Hash()\n"
    "ok = Variable()\n\n"
    "@export\ndef hash_key():\n    data['x' * 494] = 1\n"
    "@export\ndef mutate_key():\n    data['x' * 494] += 1\n"
    "@export\ndef utf8_key():\n    data['\\u00e9' * 247] = 1\n"
    "@export\ndef multi_key():\n    data['x' * 246, 'y' * 247] = 1\n"
    f"@export\ndef clone_key():\n    data['k' * 400] = 1\n    {_TARGET}.clone_from(data)\n"
    "@export\ndef largest_key():\n    data['x' * 493] = 1\n"
    "@export\ndef good():\n    ok.set(7)\n"
)


@pytest.mark.parametrize(
    "function", ["hash_key", "mutate_key", "utf8_key", "multi_key", "clone_key"]
)
def test_finalize_fails_a_write_lmdb_cannot_store_and_commits_the_block(tmp_path, function):
    async def run():
        async with isolated_app(tmp_path, STORAGE_KEY_SOURCE) as app:
            txs = [
                signed_tx(app, function=function, nonce=0),
                signed_tx(app, function="good", nonce=1),
            ]
            result = await app.finalize_block(
                RequestFinalizeBlock(height=1, hash=b"1" * 32, txs=txs)
            )
            assert [tx.code for tx in result.tx_results] == [1, 0]
            failed = json.loads(result.tx_results[0].data)
            assert "511" in failed["result"]
            assert failed["chi_used"] > 0
            assert not any(write["key"].startswith("con_boundary.") for write in failed["state"])
            await app.commit()
            assert app.client.raw_driver.get("con_boundary.ok") == 7
            assert StateRootCache.from_driver(app.client.raw_driver).root_hash == result.app_hash

    asyncio.run(run())


def test_finalize_commits_the_largest_storable_key(tmp_path):
    async def run():
        async with isolated_app(tmp_path, STORAGE_KEY_SOURCE) as app:
            result = await app.finalize_block(
                RequestFinalizeBlock(
                    height=1, hash=b"1" * 32, txs=[signed_tx(app, function="largest_key", nonce=0)]
                )
            )
            assert result.tx_results[0].code == 0
            await app.commit()
            app.client.raw_driver.flush_cache()
            key = "con_boundary.data:" + "x" * 493
            assert len(key.encode()) == 511
            assert app.client.raw_driver.get(key) == 1

    asyncio.run(run())


def test_finalize_refuses_a_block_state_root_over_a_key_lmdb_cannot_store(tmp_path):
    # Defense in depth: a pending write that bypassed every write check still
    # never gets an app hash, so it can never fail later, at Commit.
    async def run():
        async with isolated_app(tmp_path, STORAGE_KEY_SOURCE) as app:
            # The block overlay itself does not check keys; stage one directly.
            app.client.raw_driver._pending.write("__local." + "x" * 504, b"1")
            with pytest.raises(ValueError, match="Max is 511"):
                await app.finalize_block(
                    RequestFinalizeBlock(
                        height=1, hash=b"1" * 32, txs=[signed_tx(app, function="good", nonce=0)]
                    )
                )
            assert app.merkle_root_hash is None

    asyncio.run(run())
