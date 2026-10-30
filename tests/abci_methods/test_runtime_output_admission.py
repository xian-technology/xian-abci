import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from cometbft.abci.v1beta2.types_pb2 import ResponseProcessProposal
from cometbft.abci.v1beta3.types_pb2 import (
    RequestFinalizeBlock,
    RequestPrepareProposal,
    RequestProcessProposal,
)
from xian.constants import Constants
from xian.fee_policy import TxFeePolicy
from xian.state_root import StateRootCache
from xian.utils import tx as tx_validation
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
def test_finalize_rejects_invalid_state_and_commits_following_transaction(tmp_path, expression):
    source = (
        "value = Variable()\nvalid = Variable()\n\n"
        "@export\ndef invalid():\n    valid.set(99)\n"
        f"    value.set({expression})\n\n"
        "@export\ndef good():\n    valid.set(7)\n    return {1: 2}\n"
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


@pytest.mark.parametrize("policy", [TxFeePolicy.paid_metered(), TxFeePolicy.free_metered()])
@pytest.mark.parametrize("native_admission", [False, True])
def test_zero_chi_is_rejected_by_all_transaction_entrypoints(
    tmp_path, policy, native_admission, monkeypatch
):
    if native_admission:
        if tx_validation._native_decode_and_validate_transaction_static is None:
            pytest.skip("native admission helper is not installed")
    else:
        monkeypatch.setattr(tx_validation, "_native_decode_and_validate_transaction_static", None)

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
