from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from xian_runtime_types.time import Datetime

from contracting.client import ContractingClient

from xian.execution_engine import build_execution_runtime
from xian.execution_policy import ExecutionPolicy
from xian.processor import TxProcessor

pytest.importorskip("xian_vm_core")

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
IGNORED_BALANCE_KEY = "currency.balances:alice"


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


def _normalized_state_writes(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if entries is None:
        return None
    return [
        entry
        for entry in entries
        if entry.get("key") != "submission.__submitted__"
        and entry.get("key") != IGNORED_BALANCE_KEY
        and not str(entry.get("key", "")).endswith(".__code__")
    ]


def _normalized_contract_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(state)
    normalized.pop("submission.__submitted__", None)
    normalized.pop(IGNORED_BALANCE_KEY, None)
    for key in list(normalized):
        if key.endswith(".__code__"):
            normalized.pop(key, None)
    return normalized


def _normalized_tx_result(tx_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if tx_result is None:
        return None
    return {
        "status": tx_result.get("status"),
        "hash": tx_result.get("hash"),
        "result": tx_result.get("result"),
        "state": _normalized_state_writes(tx_result.get("state")),
        "events": tx_result.get("events"),
    }


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
        value=st.integers(min_value=-20, max_value=20),
    ),
    st.builds(
        lambda key, delta: ("add", {"key": key, "delta": delta}),
        key=st.sampled_from(["alpha", "beta", "gamma", "delta"]),
        delta=st.integers(min_value=-6, max_value=8),
    ),
    st.builds(
        lambda slot, amount: ("claim", {"slot": slot, "amount": amount}),
        slot=st.sampled_from(["slot-a", "slot-b", "slot-c", "slot-d"]),
        amount=st.integers(min_value=0, max_value=6),
    ),
    st.builds(
        lambda key, value: ("fail_after_write", {"key": key, "value": value}),
        key=st.sampled_from(["alpha", "beta", "gamma", "delta"]),
        value=st.integers(min_value=-20, max_value=20),
    ),
)


@settings(max_examples=24, deadline=None)
@given(operations=st.lists(FUZZ_OPERATION, min_size=1, max_size=12))
def test_python_and_native_processors_match_for_fuzzed_sequences(
    operations: list[tuple[str, dict[str, Any]]],
) -> None:
    with tempfile.TemporaryDirectory(prefix="xian-vm-processor-fuzz-") as tmpdir:
        root = Path(tmpdir)
        python_client = ContractingClient(storage_home=root / "python")
        native_client = ContractingClient(storage_home=root / "native")
        _deploy_probe(python_client)
        _deploy_probe(native_client)

        python_processor = TxProcessor(client=python_client)
        native_processor = TxProcessor(
            client=native_client,
            execution_runtime=build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="native",
                )
            ),
        )

        for offset_seconds, (function_name, kwargs) in enumerate(operations, start=1):
            before_state = _normalized_contract_state(
                python_client.raw_driver.get_all_contract_state()
            )
            assert before_state == _normalized_contract_state(
                native_client.raw_driver.get_all_contract_state()
            )

            tx = _make_tx(function_name, kwargs, offset_seconds)
            python_result = python_processor.process_tx(
                tx=tx,
                enabled_fees=False,
            )
            native_result = native_processor.process_tx(
                tx=tx,
                enabled_fees=False,
            )

            assert _normalized_tx_result(python_result["tx_result"]) == _normalized_tx_result(
                native_result["tx_result"]
            )

            block_nanos = tx["b_meta"]["nanos"]
            python_client.raw_driver.hard_apply(block_nanos)
            native_client.raw_driver.hard_apply(block_nanos)

            after_python_state = _normalized_contract_state(
                python_client.raw_driver.get_all_contract_state()
            )
            after_native_state = _normalized_contract_state(
                native_client.raw_driver.get_all_contract_state()
            )
            assert after_python_state == after_native_state

            tx_result = python_result["tx_result"]
            if tx_result is not None and tx_result.get("status") != 0:
                assert after_python_state == before_state


def test_processors_preserve_failure_rollback_invariant() -> None:
    with tempfile.TemporaryDirectory(prefix="xian-vm-processor-failure-") as tmpdir:
        root = Path(tmpdir)
        python_client = ContractingClient(storage_home=root / "python")
        native_client = ContractingClient(storage_home=root / "native")
        _deploy_probe(python_client)
        _deploy_probe(native_client)

        python_processor = TxProcessor(client=python_client)
        native_processor = TxProcessor(
            client=native_client,
            execution_runtime=build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="native",
                )
            ),
        )

        before_state = _normalized_contract_state(
            python_client.raw_driver.get_all_contract_state()
        )
        tx = _make_tx("fail_after_write", {"key": "alpha", "value": 7}, 1)
        python_result = python_processor.process_tx(tx=tx, enabled_fees=False)
        native_result = native_processor.process_tx(tx=tx, enabled_fees=False)

        assert _normalized_tx_result(
            python_result["tx_result"]
        ) == _normalized_tx_result(native_result["tx_result"])
        assert python_result["tx_result"] is not None
        assert python_result["tx_result"]["status"] == 1
        assert _normalized_tx_result(python_result["tx_result"])["state"] == []
        assert _normalized_tx_result(python_result["tx_result"])["events"] == []

        block_nanos = tx["b_meta"]["nanos"]
        python_client.raw_driver.hard_apply(block_nanos)
        native_client.raw_driver.hard_apply(block_nanos)
        assert _normalized_contract_state(
            python_client.raw_driver.get_all_contract_state()
        ) == before_state
        assert _normalized_contract_state(
            native_client.raw_driver.get_all_contract_state()
        ) == before_state
