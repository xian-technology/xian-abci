"""The node forwards its transaction seed to the VM's random syscalls.

The seed is the block's chain id, height and hash and the transaction's
input hash (SHA3-256 of the block nanos and the transaction signature), read
by the block executor from the transaction bytes.
"""

import hashlib
from types import SimpleNamespace

import pytest
from contracting.storage.driver import Driver
from xian_runtime_types.encoding import decode_wire_numbers

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.tx import transaction_bytes

pytest.importorskip("xian_vm_core._native")

SOURCE = """state = Variable()
@export
def probe():
    random.seed()
    value = random.getrandbits(64)
    state.set(value)
    return value
"""
NANOS = 1770000000000000000
META = {"nanos": NANOS, "chain_id": "local-rng-audit", "hash": "abc123", "height": 17}


def _tx(signature):
    return {
        "payload": {
            "sender": "alice",
            "contract": "con_rng",
            "function": "probe",
            "kwargs": {},
            "nonce": 0,
            "chi_supplied": 100_000,
        },
        "metadata": {"signature": signature},
        "b_meta": META,
    }


def _expected(signature):
    input_hash = hashlib.sha3_256(f"{NANOS}{signature}".encode()).hexdigest()
    seed = (
        f"chain_id={META['chain_id']}|block_num={META['height']}|"
        f"block_hash={META['hash']}|input_hash={input_hash}|0"
    )
    return int.from_bytes(hashlib.sha3_256(seed.encode()).digest()[:8], "big")


@pytest.mark.parametrize(
    "policy", [TxFeePolicy.system_unmetered(), TxFeePolicy.free_metered()], ids=["free", "metered"]
)
def test_the_transaction_seed_reaches_the_random_syscalls(tmp_path, policy):
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set_contract_from_source("con_rng", SOURCE, owner=None)
        driver.commit()
        processor = TxProcessor(SimpleNamespace(raw_driver=driver))
        draws = []
        for signature in ("signature-one", "signature-two"):
            tx = _tx(signature)
            [response] = processor.process_block([tx], [transaction_bytes(tx)], fee_policy=policy)
            result = response["tx_result"]
            assert result["status"] == 0, result["result"]
            draw = decode_wire_numbers(result["result"])
            assert draw == _expected(signature)
            assert result["state"] == [{"key": "con_rng.state", "value": draw}]
            draws.append(draw)
            driver.commit()
        assert draws[0] != draws[1]
