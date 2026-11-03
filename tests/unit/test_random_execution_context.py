"""The node forwards its transaction seed through the real native boundary."""

import hashlib

import pytest
from contracting.execution.runtime import rt
from contracting.storage.driver import Driver

from xian.execution_engine import VmRuntime, execute_vm_transaction
from xian.processor import TxProcessor

pytest.importorskip("xian_vm_core._native")

SOURCE = """state = Variable()
@export
def probe():
    random.seed()
    value = random.getrandbits(64)
    state.set(value)
    return value
"""


def environment(signature):
    processor = object.__new__(TxProcessor)
    processor.execution_runtime = VmRuntime()
    return processor.get_environment(
        {
            "b_meta": {
                "nanos": 1770000000000000000,
                "chain_id": "local-rng-audit",
                "hash": "abc123",
                "height": 17,
            },
            "metadata": {"signature": signature},
        }
    )


@pytest.mark.parametrize("meter", [False, True])
def test_transaction_hash_is_used_and_ambient_state_is_irrelevant(tmp_path, meter):
    previous = dict(rt.env)
    try:
        with Driver(storage_home=tmp_path) as driver:
            driver.set_contract_from_source("con_rng", SOURCE, owner=None)
            driver.commit()
            draws = []
            for signature in ("signature-one", "signature-two"):
                explicit = environment(signature)
                seed = (
                    f"chain_id={explicit['chain_id']}|block_num={explicit['block_num']}|"
                    f"block_hash={explicit['block_hash']}|input_hash={explicit['__input_hash']}|0"
                )
                expected = int.from_bytes(hashlib.sha3_256(seed.encode()).digest()[:8], "big")
                observations = []
                for ambient in ({}, {"__input_hash": "ambient-a"}, {"__input_hash": "ambient-b"}):
                    rt.env = ambient
                    output = execute_vm_transaction(
                        VmRuntime(),
                        driver,
                        sender="alice",
                        contract_name="con_rng",
                        function_name="probe",
                        kwargs={},
                        environment=explicit,
                        meter=meter,
                        chi_budget=1_000_000,
                        chi_cost=1,
                        apply_metering_writes=False,
                    )
                    assert output.output.status_code == 0, output.output.result
                    assert output.output.result == expected
                    assert output.writes == {"con_rng.state": expected}
                    assert dict(rt.env) == ambient
                    observations.append((output.writes, output.chi_used, output.contract_costs))
                assert observations[0] == observations[1] == observations[2]
                draws.append(expected)
            assert draws[0] != draws[1]
    finally:
        rt.env = previous
