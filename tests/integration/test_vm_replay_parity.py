import ast
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from contracting.local import ContractingClient
from processor_probe_model import typed_value

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
    dt = datetime(2026, 4, 12, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds)
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


def probe_event(*, key, value, counter):
    return {
        "contract": "con_vm_parity_probe",
        "event": "Probe",
        "signer": "alice",
        "caller": "alice",
        "data_indexed": {"key": key},
        "data": {"value": value, "counter": counter},
    }


def probe_receipt(counter, *, key=None, value=None):
    writes = {"con_vm_parity_probe.counter": counter}
    if key is None:
        result, events = counter, []
    else:
        writes[f"con_vm_parity_probe.items:{key}"] = value
        result = {"counter": counter, "item": value}
        events = [probe_event(key=key, value=value, counter=counter)]
    return {
        "status": 0,
        "result": result,
        "state": [{"key": key, "value": writes[key]} for key in sorted(writes)],
        "events": events,
    }


class TestNativeReplayDeterminism(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="xian-native-replay-")
        self.addCleanup(self._tmpdir.cleanup)
        root = Path(self._tmpdir.name)
        self.homes = [root / "original", root / "clean-replay"]

    def make_client(self, home):
        client = ContractingClient(vm_profile="xian_vm_v2", storage_home=home)
        self.addCleanup(client.close)
        client.submit(PARITY_PROBE_SOURCE, name="con_vm_parity_probe")
        client.raw_driver.commit()
        return client

    def assert_semantics(self, actual, expected):
        self.assertIsNotNone(actual)
        self.assertEqual(typed_value({key: actual[key] for key in expected}), typed_value(expected))
        # These system-unmetered calls do not exercise submitted fee budgets.
        self.assertIsNone(actual["rewards"])
        self.assertIsNone(actual["reward_records"])

    def persisted_state(self, home):
        contract_state, run_state = fetch_filebased_state(storage_home=home)
        # Only the built-in helper's installation time differs between homes.
        contract_state.pop("submission.__submitted__", None)
        return contract_state, run_state

    def test_clean_native_replay_matches_explicit_results_and_committed_state(self):
        operations = [
            ("set_item", {"key": "alpha", "value": 5}),
            ("accumulate", {"values": [1, 2, 3]}),
            ("set_item", {"key": "beta", "value": -1}),
            ("set_item", {"key": "beta", "value": 7}),
        ]
        expected = [
            probe_receipt(5, key="alpha", value=5),
            probe_receipt(11),
            {
                "status": 1,
                "result": "AssertionError('value must be non-negative.')",
                "state": [],
                "events": [],
            },
            probe_receipt(18, key="beta", value=7),
        ]
        replays = []
        for home in self.homes:
            with self.make_client(home) as client:
                processor = TxProcessor(client=client, execution_runtime=build_vm_runtime())
                expected_state = client.raw_driver.items()
                receipts = []
                for offset, ((function, kwargs), receipt) in enumerate(
                    zip(operations, expected), 1
                ):
                    tx = make_tx(
                        sender="alice", function=function, kwargs=kwargs, offset_seconds=offset
                    )
                    actual = processor.process_tx(tx=tx)["tx_result"]
                    self.assert_semantics(actual, receipt)
                    expected_state.update(
                        {entry["key"]: entry["value"] for entry in receipt["state"]}
                    )
                    client.raw_driver.commit()
                    self.assertEqual(
                        typed_value(client.raw_driver.items()), typed_value(expected_state)
                    )
                    receipts.append(actual)
            replays.append((receipts, self.persisted_state(home)))
        # This compares independent clean runs for determinism, after both have
        # already satisfied explicit outcomes and full-state/rollback checks.
        self.assertEqual(typed_value(replays[0]), typed_value(replays[1]))

    def assert_deployment(self, result):
        self.assert_semantics(
            result,
            {
                "status": 0,
                "result": None,
                "events": [
                    {
                        "contract": "submission",
                        "event": "ContractDeployed",
                        "signer": "alice",
                        "caller": "alice",
                        "data_indexed": {"name": "con_vm_replay_child", "developer": "alice"},
                        "data": {"owner": ""},
                    }
                ],
            },
        )
        writes = {
            entry["key"].removeprefix("con_vm_replay_child."): entry["value"]
            for entry in result["state"]
        }
        self.assertEqual(
            set(writes),
            {
                "__deployer__",
                "__developer__",
                "__initiator__",
                "__owner__",
                "__source__",
                "__submitted__",
                "__xian_ir_v1__",
                "counter",
            },
        )
        self.assertEqual(writes["counter"], 3)
        for key in ("__deployer__", "__developer__", "__initiator__"):
            self.assertEqual(writes[key], "alice")
        self.assertIsNone(writes["__owner__"])
        self.assertEqual(str(writes["__submitted__"]), "2026-04-12 12:00:01")
        self.assertEqual(
            ast.dump(ast.parse(writes["__source__"])), ast.dump(ast.parse(DEPLOYED_PROBE_SOURCE))
        )
        ir = json.loads(writes["__xian_ir_v1__"])
        self.assertEqual(ir["module_name"], "con_vm_replay_child")
        self.assertEqual(ir["vm_profile"], "xian_vm_v2")
        self.assertEqual(
            {function["name"]: function["visibility"] for function in ir["functions"]},
            {"seed": "construct", "bump": "export"},
        )

    def test_clean_native_replay_preserves_deployment_constructor_and_followup_calls(self):
        deployment = make_tx(
            sender="alice",
            contract="submission",
            function="submit_contract",
            kwargs={
                "name": "con_vm_replay_child",
                "code": DEPLOYED_PROBE_SOURCE,
                "constructor_args": {"start": 3},
            },
            offset_seconds=1,
        )
        replays = []
        for home in self.homes:
            with self.make_client(home) as client:
                processor = TxProcessor(client=client, execution_runtime=build_vm_runtime())
                initial = client.raw_driver.items()
                deployed = processor.process_tx(tx=deployment)["tx_result"]
                self.assert_deployment(deployed)
                expected_state = initial | {
                    entry["key"]: entry["value"]
                    for entry in deployed["state"]
                    if entry["value"] is not None
                }
                client.raw_driver.commit()
                self.assertEqual(
                    typed_value(client.raw_driver.items()), typed_value(expected_state)
                )
                receipts = [deployed]
                for offset, delta, expected_value in [(2, 2, 5), (3, 5, 10)]:
                    tx = make_tx(
                        sender="alice",
                        contract="con_vm_replay_child",
                        function="bump",
                        kwargs={"delta": delta},
                        offset_seconds=offset,
                    )
                    actual = processor.process_tx(tx=tx)["tx_result"]
                    self.assert_semantics(
                        actual,
                        {
                            "status": 0,
                            "result": expected_value,
                            "state": [
                                {"key": "con_vm_replay_child.counter", "value": expected_value}
                            ],
                            "events": [],
                        },
                    )
                    expected_state["con_vm_replay_child.counter"] = expected_value
                    client.raw_driver.commit()
                    self.assertEqual(
                        typed_value(client.raw_driver.items()), typed_value(expected_state)
                    )
                    receipts.append(actual)
            replays.append((receipts, self.persisted_state(home)))
        self.assertEqual(typed_value(replays[0]), typed_value(replays[1]))


if __name__ == "__main__":
    unittest.main()
