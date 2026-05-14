import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from contracting.artifacts import build_contract_artifacts
from contracting.local import ContractingClient

from xian.execution_engine import build_vm_runtime
from xian.processor import TxProcessor
from xian.state_export import fetch_filebased_state

PARITY_PROBE_SOURCE = """
counter = Variable()
items = Hash(default_value=0)

ProbeEvent = LogEvent(
    "Probe",
    {
        "key": indexed(str),
        "value": int,
        "counter": int,
    },
)


@construct
def seed():
    counter.set(0)


@export
def set_item(key: str, value: int):
    assert isinstance(key, str) and key != "", "key must be non-empty."
    assert isinstance(value, int), "value must be an int."
    assert value >= 0, "value must be non-negative."
    items[key] = value
    next_value = counter.get() + value
    counter.set(next_value)
    ProbeEvent({"key": key, "value": value, "counter": next_value})
    return {"counter": next_value, "item": items[key]}


@export
def accumulate(values: list):
    total = 0
    for value in values:
        total += value
    counter.set(counter.get() + total)
    return counter.get()
""".strip()

DEPLOYED_PROBE_SOURCE = """
counter = Variable()


@construct
def seed(start: int = 0):
    counter.set(start)


@export
def bump(delta: int):
    updated = counter.get() + delta
    counter.set(updated)
    return updated
""".strip()


def make_block_meta(offset_seconds: int) -> dict:
    dt = datetime(2026, 4, 12, 12, 0, 0, tzinfo=UTC) + timedelta(
        seconds=offset_seconds
    )
    nanos = int(dt.timestamp()) * 1_000_000_000
    return {
        "nanos": nanos,
        "height": 100 + offset_seconds,
        "chain_id": "xian-local",
        "hash": f"block-{offset_seconds:02d}",
    }


def make_tx(
    *,
    sender: str,
    function: str,
    kwargs: dict,
    offset_seconds: int,
    contract: str = "con_vm_parity_probe",
) -> dict:
    return {
        "payload": {
            "contract": contract,
            "function": function,
            "sender": sender,
            "kwargs": kwargs,
            "chi_supplied": 1_000,
        },
        "metadata": {"signature": "abc"},
        "b_meta": make_block_meta(offset_seconds),
    }


class TestVmReplayParity(unittest.TestCase):
    @staticmethod
    def _normalized_state_writes(entries: list[dict] | None) -> list[dict] | None:
        if entries is None:
            return None
        return [
            entry
            for entry in entries
            if entry.get("key") != "submission.__submitted__"
        ]

    @staticmethod
    def _normalized_contract_state(state: dict) -> dict:
        normalized = dict(state)
        # Each isolated storage home bootstraps the built-in submission contract
        # independently, so its installation timestamp is not part of replay parity.
        normalized.pop("submission.__submitted__", None)
        return normalized

    @staticmethod
    def _normalized_tx_result(tx_result: dict) -> dict:
        return {
            "status": tx_result.get("status"),
            "hash": tx_result.get("hash"),
            "result": tx_result.get("result"),
            "state": TestVmReplayParity._normalized_state_writes(
                tx_result.get("state")
            ),
            "events": tx_result.get("events"),
        }

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="xian-vm-parity-")
        root = Path(self._tmpdir.name)
        self.python_home = root / "python"
        self.native_home = root / "native"
        self.python_home.mkdir(parents=True, exist_ok=True)
        self.native_home.mkdir(parents=True, exist_ok=True)

        self.python_client = ContractingClient(storage_home=self.python_home)
        self.native_client = ContractingClient(storage_home=self.native_home)
        self.python_client.submit(PARITY_PROBE_SOURCE, name="con_vm_parity_probe")
        self.native_client.submit(PARITY_PROBE_SOURCE, name="con_vm_parity_probe")
        self.python_client.raw_driver.hard_apply(0)
        self.native_client.raw_driver.hard_apply(0)

        self.python_processor = TxProcessor(client=self.python_client)
        self.native_processor = TxProcessor(
            client=self.native_client,
            execution_runtime=build_vm_runtime(),
        )

    def tearDown(self):
        self.python_client.flush()
        self.native_client.flush()
        self._tmpdir.cleanup()

    def test_python_and_native_processors_replay_the_same_tx_sequence(self):
        txs = [
            make_tx(
                sender="alice",
                function="set_item",
                kwargs={"key": "alpha", "value": 5},
                offset_seconds=1,
            ),
            make_tx(
                sender="alice",
                function="accumulate",
                kwargs={"values": [1, 2, 3]},
                offset_seconds=2,
            ),
            make_tx(
                sender="alice",
                function="set_item",
                kwargs={"key": "beta", "value": -1},
                offset_seconds=3,
            ),
            make_tx(
                sender="alice",
                function="set_item",
                kwargs={"key": "beta", "value": 7},
                offset_seconds=4,
            ),
        ]

        for tx in txs:
            python_result = self.python_processor.process_tx(
                tx=tx,
                enabled_fees=False,
            )
            native_result = self.native_processor.process_tx(
                tx=tx,
                enabled_fees=False,
            )

            self.assertEqual(
                self._normalized_tx_result(python_result["tx_result"]),
                self._normalized_tx_result(native_result["tx_result"]),
            )
            block_nanos = tx["b_meta"]["nanos"]
            self.python_client.raw_driver.hard_apply(block_nanos)
            self.native_client.raw_driver.hard_apply(block_nanos)

        python_contract_state, python_run_state = fetch_filebased_state(
            storage_home=self.python_home
        )
        native_contract_state, native_run_state = fetch_filebased_state(
            storage_home=self.native_home
        )

        self.assertEqual(
            self._normalized_contract_state(python_contract_state),
            self._normalized_contract_state(native_contract_state),
        )
        self.assertEqual(python_run_state, native_run_state)

    def test_python_and_native_processors_replay_submission_deployment_sequence(
        self,
    ):
        artifacts = build_contract_artifacts(
            module_name="con_vm_replay_child",
            source=DEPLOYED_PROBE_SOURCE,
        )
        txs = [
            make_tx(
                sender="alice",
                contract="submission",
                function="submit_contract",
                kwargs={
                    "name": "con_vm_replay_child",
                    "deployment_artifacts": artifacts,
                    "constructor_args": {"start": 3},
                },
                offset_seconds=1,
            ),
            make_tx(
                sender="alice",
                contract="con_vm_replay_child",
                function="bump",
                kwargs={"delta": 2},
                offset_seconds=2,
            ),
            make_tx(
                sender="alice",
                contract="con_vm_replay_child",
                function="bump",
                kwargs={"delta": 5},
                offset_seconds=3,
            ),
        ]

        for tx in txs:
            python_result = self.python_processor.process_tx(
                tx=tx,
                enabled_fees=False,
            )
            native_result = self.native_processor.process_tx(
                tx=tx,
                enabled_fees=False,
            )

            self.assertEqual(
                self._normalized_tx_result(python_result["tx_result"]),
                self._normalized_tx_result(native_result["tx_result"]),
            )

            block_nanos = tx["b_meta"]["nanos"]
            self.python_client.raw_driver.hard_apply(block_nanos)
            self.native_client.raw_driver.hard_apply(block_nanos)

        python_contract_state, python_run_state = fetch_filebased_state(
            storage_home=self.python_home
        )
        native_contract_state, native_run_state = fetch_filebased_state(
            storage_home=self.native_home
        )

        self.assertEqual(
            self._normalized_contract_state(python_contract_state),
            self._normalized_contract_state(native_contract_state),
        )
        self.assertEqual(python_run_state, native_run_state)


if __name__ == "__main__":
    unittest.main()
