from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from contracting.local import ContractingClient
from hypothesis import given, settings
from hypothesis import strategies as st
from processor_probe_model import ProcessorProbeModel, typed_value
from xian_runtime_types.time import Datetime

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.tx import transaction_bytes

pytest.importorskip("xian_vm_core")
def process_tx(processor, tx):
    """The block executor's response for ``tx`` alone in an unmetered block."""
    [response] = processor.process_block(
        [tx], [transaction_bytes(tx)], fee_policy=TxFeePolicy.system_unmetered()
    )
    return response


PROCESSOR_FUZZ_SOURCE = """
counter = Variable(default_value=0)
items = Hash(default_value=0)
claims = Hash(default_value=False)

ProbeEvent = LogEvent(
    "Probe",
    {
        "kind": indexed(str),
        "key": indexed(str),
        "value": int,
        "counter": int,
    },
)

@export
def put(key: str, value: int):
    assert isinstance(key, str) and key != "", "key must be non-empty."
    assert -50 <= value <= 50, "value out of range."
    items[key] = value
    counter.set(counter.get() + 1)
    ProbeEvent(
        {
            "kind": "put",
            "key": key,
            "value": value,
            "counter": counter.get(),
        }
    )
    return {"counter": counter.get(), "value": items[key]}


@export
def add(key: str, delta: int):
    updated = items[key] + delta
    assert -100 <= updated <= 100, "updated value out of range."
    items[key] = updated
    counter.set(counter.get() + 1)
    ProbeEvent(
        {
            "kind": "add",
            "key": key,
            "value": updated,
            "counter": counter.get(),
        }
    )
    return updated


@export
def claim(slot: str, amount: int):
    assert isinstance(slot, str) and slot != "", "slot must be non-empty."
    assert amount >= 0, "amount must be non-negative."
    assert not claims[slot], "slot already claimed."
    claims[slot] = True
    counter.set(counter.get() + amount)
    ProbeEvent(
        {
            "kind": "claim",
            "key": slot,
            "value": amount,
            "counter": counter.get(),
        }
    )
    return counter.get()


@export
def fail_after_write(key: str, value: int):
    items[key] = value
    ProbeEvent(
        {
            "kind": "fail",
            "key": key,
            "value": value,
            "counter": counter.get(),
        }
    )
    assert False, "forced failure"
""".strip()

FIXED_TIMESTAMP = Datetime(2026, 4, 23, 12, 0, 0)
FIXED_BLOCK_TIME = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)


def _make_block_meta(offset_seconds: int) -> dict[str, Any]:
    block_time = FIXED_BLOCK_TIME + timedelta(seconds=offset_seconds)
    nanos_int = int(block_time.timestamp()) * 1_000_000_000
    return {
        "nanos": nanos_int,
        "height": 100 + offset_seconds,
        "chain_id": "xian-local",
        "hash": f"block-{offset_seconds:02d}",
    }


def _make_tx(function: str, kwargs: dict[str, Any], offset_seconds: int) -> dict[str, Any]:
    return {
        "payload": {
            "contract": "con_vm_processor_fuzz",
            "function": function,
            "sender": "alice",
            "kwargs": kwargs,
            "chi_supplied": 2_000,
        },
        "metadata": {"signature": "abc"},
        "b_meta": _make_block_meta(offset_seconds),
    }


def _semantic_receipt(tx_result: dict[str, Any]) -> dict[str, Any]:
    return {key: tx_result[key] for key in ("status", "result", "state", "events")}


def _deploy_probe(client: ContractingClient) -> None:
    client.raw_driver.set_contract_from_source(
        "con_vm_processor_fuzz",
        PROCESSOR_FUZZ_SOURCE,
        owner="alice",
        overwrite=True,
        timestamp=FIXED_TIMESTAMP,
    )
    client.raw_driver.commit()


FUZZ_OPERATION = st.one_of(
    st.builds(
        lambda key, value: ("put", {"key": key, "value": value}),
        key=st.sampled_from(["alpha", "beta", "gamma", "delta"]),
        value=st.integers(min_value=-60, max_value=60),
    ),
    st.builds(
        lambda key, delta: ("add", {"key": key, "delta": delta}),
        key=st.sampled_from(["alpha", "beta", "gamma", "delta"]),
        delta=st.integers(min_value=-120, max_value=120),
    ),
    st.builds(
        lambda slot, amount: ("claim", {"slot": slot, "amount": amount}),
        slot=st.sampled_from(["slot-a", "slot-b", "slot-c", "slot-d"]),
        amount=st.integers(min_value=-2, max_value=6),
    ),
    st.builds(
        lambda key, value: ("fail_after_write", {"key": key, "value": value}),
        key=st.sampled_from(["alpha", "beta", "gamma", "delta"]),
        value=st.integers(min_value=-60, max_value=60),
    ),
)


def _check_sequence(operations):
    with tempfile.TemporaryDirectory(prefix="xian-vm-processor-model-") as tmpdir:
        with ContractingClient(vm_profile="xian_vm_v2", storage_home=Path(tmpdir)) as client:
            _deploy_probe(client)
            processor = TxProcessor(client=client)
            model = ProcessorProbeModel()
            deployment_state = client.raw_driver.items()
            for offset, (function, kwargs) in enumerate(operations, start=1):
                expected = model.step(function, kwargs)
                tx = _make_tx(function, kwargs, offset)
                tx_result = process_tx(processor, tx)["tx_result"]
                assert tx_result is not None
                assert typed_value(_semantic_receipt(tx_result)) == typed_value(expected)
                # These calls use system_unmetered; fee/reward behavior is covered
                # separately. No expected cost is derived from the VM's output.
                assert tx_result["rewards"] is None
                assert tx_result["reward_records"] is None
                client.raw_driver.commit()
                assert typed_value(client.raw_driver.items()) == typed_value(
                    deployment_state | model.state
                )
            committed_state = deployment_state | model.state
        with ContractingClient(
            vm_profile="xian_vm_v2", storage_home=Path(tmpdir), submission_filename=None
        ) as reopened:
            assert typed_value(reopened.raw_driver.items()) == typed_value(committed_state)


@settings(max_examples=24, deadline=None)
@given(operations=st.lists(FUZZ_OPERATION, min_size=1, max_size=12))
def test_native_processor_matches_independent_state_model(operations) -> None:
    _check_sequence(operations)


def test_native_processor_preserves_success_failure_and_followup_semantics() -> None:
    _check_sequence(
        [
            ("put", {"key": "alpha", "value": 5}),
            ("add", {"key": "alpha", "delta": 2}),
            ("claim", {"slot": "slot-a", "amount": 3}),
            ("claim", {"slot": "slot-a", "amount": 1}),
            ("fail_after_write", {"key": "alpha", "value": 99}),
            ("put", {"key": "beta", "value": -3}),
            ("add", {"key": "beta", "delta": 1000}),
            ("put", {"key": "", "value": 1}),
            ("put", {"key": "beta", "value": 99}),
            ("claim", {"slot": "slot-b", "amount": -1}),
            ("claim", {"slot": "", "amount": 1}),
            ("add", {"key": "beta", "delta": 3}),
        ]
    )
