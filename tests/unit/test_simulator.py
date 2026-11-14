import asyncio
import decimal
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from contracting.constants import TRANSACTION_BASE_CHI
from contracting.local import ContractingClient
from contracting.runtime_features import set_driver_runtime_features
from xian_runtime_types.decimal import ContractingDecimal
from xian_vm_core import HandedBack, TxRun

from xian.execution_engine import build_vm_runtime, execute_vm_transaction
from xian.simulator import (
    PreparedSimulation,
    QuerySimulationService,
    TransactionSimulator,
    _estimated_transaction_size,
    estimate_to_fixpoint,
)
from xian.utils.block import set_latest_block_nanos
from xian.utils.tx import canonical_transaction_size_bytes


def _driver(storage_home: Path | None = None):
    return SimpleNamespace(storage_home=storage_home or Path("/tmp"))


def _simulator(driver=None, *, chain_id: str | None = None):
    simulator = object.__new__(TransactionSimulator)
    simulator.client = SimpleNamespace(raw_driver=driver or _driver())
    simulator.execution_runtime = SimpleNamespace(mode="xian_vm_v2")
    simulator.get_block_meta = lambda: None
    simulator.chain_id = chain_id
    simulator.charge_fees = True
    return simulator


def _run(result, *, status_code=0, chi_used=255, writes=None):
    return TxRun(
        status_code=status_code,
        result=result,
        writes=dict(writes or {}),
        events=[],
        chi_used=chi_used,
        contract_costs={},
        unprepared=False,
    )


PAYLOAD = {"sender": "alice", "contract": "con_demo", "function": "f", "kwargs": {}}


@pytest.fixture
def chain():
    """A local chain with the submission contract and a chi rate."""
    client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
    client.flush()
    client.raw_driver.set("chi_cost.S:value", 20)
    client.raw_driver.set("currency.balances:alice", ContractingDecimal("100"))
    try:
        yield client
    finally:
        client.flush()
        client.close()


class SimulatorTests(unittest.TestCase):
    def test_make_environment_uses_latest_committed_block_time_when_idle(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)
            expected_nanos = 1_710_000_000_123_000_000
            set_latest_block_nanos(expected_nanos, storage_home)

            simulator = _simulator(_driver(storage_home))

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 2024)
            self.assertEqual(environment["now"].month, 3)
            self.assertEqual(environment["now"].day, 9)
            self.assertEqual(environment["now"].microsecond, 123000)

    def test_make_environment_falls_back_to_epoch_without_chain_time(self):
        with TemporaryDirectory() as tmpdir:
            simulator = _simulator(_driver(Path(tmpdir)))

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 1970)
            self.assertEqual(environment["now"].month, 1)
            self.assertEqual(environment["now"].day, 1)

    def test_make_environment_uses_configured_chain_id_when_idle(self):
        with TemporaryDirectory() as tmpdir:
            simulator = _simulator(
                _driver(Path(tmpdir)),
                chain_id="xian-local",
            )

            environment = simulator._make_environment()

            self.assertEqual(environment["chain_id"], "xian-local")

    def test_make_environment_is_deterministic_for_same_payload(self):
        simulator = _simulator()
        simulator.get_block_meta = lambda: {
            "height": 7,
            "nanos": 1_710_000_000_123_000_000,
            "chain_id": "xian-local",
        }
        payload = {
            "sender": "alice",
            "contract": "con_game",
            "function": "roll",
            "kwargs": {"sides": 6},
        }

        first = simulator._make_environment(payload)
        second = simulator._make_environment(payload)

        self.assertEqual(first["block_hash"], second["block_hash"])
        self.assertEqual(first["__input_hash"], second["__input_hash"])

    def test_make_environment_changes_input_hash_for_different_payloads(self):
        simulator = _simulator()
        simulator.get_block_meta = lambda: {
            "height": 7,
            "nanos": 1_710_000_000_123_000_000,
            "chain_id": "xian-local",
        }

        left = simulator._make_environment(
            {
                "sender": "alice",
                "contract": "con_game",
                "function": "roll",
                "kwargs": {"sides": 6},
            }
        )
        right = simulator._make_environment(
            {
                "sender": "alice",
                "contract": "con_game",
                "function": "roll",
                "kwargs": {"sides": 20},
            }
        )

        self.assertEqual(left["block_hash"], right["block_hash"])
        self.assertNotEqual(left["__input_hash"], right["__input_hash"])

    def test_make_environment_exposes_internal_execution_mode(self):
        simulator = _simulator()
        simulator.get_block_meta = lambda: {
            "height": 7,
            "nanos": 1_710_000_000_123_000_000,
            "chain_id": "xian-local",
        }

        environment = simulator._make_environment({"contract": "currency"})

        self.assertEqual(environment["__xian_execution_mode__"], "xian_vm_v2")


class SimulationResultTests(unittest.TestCase):
    """The ``simulate_tx`` response a finished run becomes."""

    def test_structured_results_are_normalized_for_abci(self):
        result = TransactionSimulator._rust_result(
            PAYLOAD, _run({"account": "alice", "left_at": None})
        )

        self.assertEqual(result["status"], 0)
        self.assertEqual(result["result"], {"account": "alice", "left_at": None})

    def test_non_json_results_are_represented_without_losing_success(self):
        results = [
            {decimal.Decimal("NaN"): "first", decimal.Decimal("NaN"): "second"},
            {7: "integer", "7": "string"},
            {1: "x" * 2000},
        ]
        for original in results:
            with self.subTest(result_type=type(original)):
                run = _run(original, chi_used=987, writes={"con_example.counter": 4})
                result = TransactionSimulator._rust_result(PAYLOAD, run)
                self.assertEqual(result["status"], 0)
                self.assertEqual(result["chi_used"], 987)
                self.assertEqual(result["state"], [{"key": "con_example.counter", "value": 4}])
                self.assertEqual(result["result"], repr(original)[:1024])
                json.dumps(result, allow_nan=False)

    def test_tuple_results_are_lists_with_wire_numbers(self):
        run = _run((1, ContractingDecimal("0.637954245540464949970792229477")), chi_used=853)

        result = TransactionSimulator._rust_result(PAYLOAD, run)

        self.assertEqual(result["result"], [1, {"__fixed__": "0.637954245540464949970792229477"}])

    def test_a_failure_is_its_exception_text(self):
        run = _run(AssertionError("boom"), status_code=1, chi_used=0)

        result = TransactionSimulator._rust_result(PAYLOAD, run)

        self.assertEqual(result["status"], 1)
        self.assertEqual(result["result"], "AssertionError('boom')")

    def test_the_receipt_keeps_the_write_order_of_the_run(self):
        run = _run(None, writes={"con_z.v": 1, "currency.balances:alice": 2})

        result = TransactionSimulator._rust_result(PAYLOAD, run)

        self.assertEqual([write["key"] for write in result["state"]], list(run.writes))


def _simulate(client, payload, **options):
    return TransactionSimulator(
        client=client, execution_runtime=build_vm_runtime(), chain_id="test-chain", **options
    ).simulate(payload)


SUBMIT_PROBE = """v = Variable()

@construct
def seed(value: int = 3):
    v.set(value)

@export
def ping():
    return v.get()
"""


def test_a_free_policy_simulation_writes_no_fee(chain):
    _simulate(
        chain,
        {
            **PAYLOAD,
            "contract": "submission",
            "function": "submit_contract",
            "kwargs": {"name": "con_probe", "code": SUBMIT_PROBE},
        },
    )
    paid = _simulate(
        chain,
        {
            **PAYLOAD,
            "contract": "submission",
            "function": "submit_contract",
            "kwargs": {"name": "con_free", "code": SUBMIT_PROBE},
        },
    )
    free = _simulate(
        chain,
        {
            **PAYLOAD,
            "contract": "submission",
            "function": "submit_contract",
            "kwargs": {"name": "con_free", "code": SUBMIT_PROBE},
        },
        charge_fees=False,
    )

    keys = {write["key"] for write in paid["state"]}
    assert "currency.balances:alice" in keys
    assert "currency.balances:alice" not in {write["key"] for write in free["state"]}
    assert free["chi_used"] == paid["chi_used"]


def test_a_submission_without_source_fails_with_the_base_charge(chain):
    result = _simulate(
        chain,
        {
            **PAYLOAD,
            "contract": "submission",
            "function": "submit_contract",
            "kwargs": {"name": "con_probe"},
        },
    )

    assert result["status"] == 1
    assert "requires source code" in result["result"]
    # The estimate reports the same base charge a real failed tx pays.
    assert result["chi_used"] == TRANSACTION_BASE_CHI


def test_a_simulated_deployment_reports_chi_and_leaves_no_state(chain):
    result = _simulate(
        chain,
        {
            **PAYLOAD,
            "sender": "sys",
            "contract": "submission",
            "function": "submit_contract",
            "kwargs": {
                "name": "con_simulated_deploy_probe",
                "code": SUBMIT_PROBE,
                "constructor_args": {"value": 9},
            },
        },
    )

    assert result["status"] == 0
    assert result["chi_used"] > 0
    writes = {item["key"]: item["value"] for item in result["state"]}
    assert writes["con_simulated_deploy_probe.v"] == 9
    assert "con_simulated_deploy_probe.__source__" in writes
    assert "con_simulated_deploy_probe.__xian_ir_v1__" in writes
    assert chain.raw_driver.get_contract_source("con_simulated_deploy_probe") is None


def test_a_large_deployment_estimate_funds_the_actual_transaction_bytes(chain):
    source = "# " + "padding" * 3000 + "\n@export\ndef ping():\n    return True\n"
    simulator = TransactionSimulator(
        client=chain, execution_runtime=build_vm_runtime(), chain_id="test-chain"
    )
    payload = {
        "sender": "sys",
        "contract": "submission",
        "function": "submit_contract",
        "kwargs": {"name": "con_large_estimate", "code": source},
    }
    estimate = simulator.simulate(payload)
    assert estimate["status"] == 0, estimate
    transaction = {
        "metadata": {"signature": "a" * 128},
        "payload": {
            **payload,
            "chain_id": "test-chain",
            "nonce": 17,
            "chi_supplied": estimate["chi_used"],
        },
    }
    actual = execute_vm_transaction(
        simulator.execution_runtime,
        chain.raw_driver,
        sender=payload["sender"],
        contract_name=payload["contract"],
        function_name=payload["function"],
        kwargs=payload["kwargs"],
        environment=simulator._make_environment(payload),
        chi_budget=estimate["chi_used"],
        chi_cost=20,
        meter=True,
        transaction_size_bytes=canonical_transaction_size_bytes(transaction),
        apply_metering_writes=False,
    )
    assert actual.output.status_code == 0, actual.output.result
    assert "con_large_estimate.__source__" in actual.writes


ZK_PROBE = """@export
def get():
    return zk.is_available()
"""


def test_simulations_judge_runtime_features_like_the_node(chain):
    # The node installs its runtime features on its driver; with zk off and
    # no state setting, a zk contract cannot run, in Rust and on the Python path.
    chain.raw_driver.set_contract_from_source("con_zkprobe", ZK_PROBE)
    set_driver_runtime_features(chain.raw_driver, {"zk": False})
    service = QuerySimulationService(client=chain, chain_id="test-chain")
    payload = json.dumps({**PAYLOAD, "contract": "con_zkprobe", "function": "get"})
    prepared = service.prepare_encoded_transaction(payload.encode().hex())

    in_rust = prepared.run_in_rust()
    on_python_path = prepared.run_on_python_path()

    assert in_rust == on_python_path
    assert in_rust["status"] == 1
    assert "zk runtime feature is disabled" in in_rust["result"]


READER = """values = Hash()

@export
def read(key: str):
    return values[key]
"""


def test_a_simulation_runs_on_a_fork_of_the_state_taken_when_prepared(chain):
    chain.raw_driver.set_contract_from_source("con_reader", READER)
    chain.raw_driver.set("con_reader.values:a", 100)
    service = QuerySimulationService(client=chain, chain_id="test-chain")
    read = {**PAYLOAD, "contract": "con_reader", "function": "read", "kwargs": {"key": "a"}}
    prepared = service.prepare_encoded_transaction(json.dumps(read).encode().hex())
    chain.raw_driver.set("con_reader.values:a", 7)

    result = asyncio.run(prepared.run())

    assert (result["status"], result["result"]) == (0, 100)
    assert chain.raw_driver.get("con_reader.values:a") == 7


def test_a_handed_back_simulation_runs_on_the_python_path(chain, monkeypatch):
    service = QuerySimulationService(client=chain, chain_id="test-chain")
    chain.raw_driver.set_contract_from_source("con_reader", READER)
    payload = json.dumps(
        {**PAYLOAD, "contract": "con_reader", "function": "read", "kwargs": {"key": "a"}}
    )
    expected = asyncio.run(service.prepare_encoded_transaction(payload.encode().hex()).run())
    monkeypatch.setattr(PreparedSimulation, "run_in_rust", lambda self: HandedBack("probe"))

    result = asyncio.run(service.prepare_encoded_transaction(payload.encode().hex()).run())

    assert result == expected


def test_a_failing_rust_run_is_a_simulation_error(chain, monkeypatch):
    def panicked(*_args, **_kwargs):
        raise RuntimeError("the transaction executor panicked: probe")

    monkeypatch.setattr("xian.simulator.execute_tx", panicked)
    service = QuerySimulationService(client=chain, chain_id="test-chain")
    chain.raw_driver.set_contract_from_source("con_reader", READER)
    payload = json.dumps(
        {**PAYLOAD, "contract": "con_reader", "function": "read", "kwargs": {"key": "a"}}
    )

    result = asyncio.run(service.prepare_encoded_transaction(payload.encode().hex()).run())

    assert result["status"] == 1
    assert result["result"] == "Simulation error: the transaction executor panicked: probe"


class _BlockingQuerySimulationService(QuerySimulationService):
    """A service whose Rust runs block on a thread until released."""

    def __init__(self, **kwargs):
        super().__init__(client=SimpleNamespace(raw_driver=_PendingDriver()), **kwargs)
        self.release = threading.Event()
        self.response = {"status": 0, "result": "100"}

    def _simulator(self, client):
        service = self

        class Simulator:
            def simulate_in_rust(self, payload, **_options):
                service.release.wait(timeout=5)
                return service.response

        return Simulator()


class _PendingDriver:
    def committed_storage(self):
        return None, SimpleNamespace(snapshot=lambda: None)


ENCODED = (
    b'{"sender":"alice","contract":"currency","function":"balance_of","kwargs":{"account":"alice"}}'
).hex()


class QuerySimulationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_disabled_result_when_simulation_is_off(self):
        service = _BlockingQuerySimulationService(enabled=False)

        result = await service.simulate_encoded_transaction(ENCODED)

        self.assertEqual(result["status"], 1)
        self.assertIn("disabled", result["result"])

    async def test_rejects_requests_above_capacity(self):
        service = _BlockingQuerySimulationService(max_concurrency=1, timeout_ms=5000)

        first = asyncio.create_task(service.simulate_encoded_transaction(ENCODED))
        await asyncio.sleep(0.05)
        second = await service.simulate_encoded_transaction(ENCODED)
        service.release.set()
        first_result = await first

        self.assertEqual(first_result["status"], 0)
        self.assertEqual(second["status"], 1)
        self.assertIn("capacity exceeded", second["result"])

    async def test_times_out_long_running_simulations(self):
        service = _BlockingQuerySimulationService(max_concurrency=1, timeout_ms=1)

        result = await service.simulate_encoded_transaction(ENCODED)

        self.assertEqual(result["status"], 1)
        self.assertIn("timed out", result["result"])
        # The run still holds its slot until it finishes.
        busy = await service.simulate_encoded_transaction(ENCODED)
        self.assertIn("capacity exceeded", busy["result"])
        service.release.set()

    async def test_finished_simulations_free_their_slot(self):
        service = _BlockingQuerySimulationService(max_concurrency=1, timeout_ms=5000)
        service.release.set()

        results = [await service.simulate_encoded_transaction(ENCODED) for _ in range(3)]

        self.assertEqual([result["status"] for result in results], [0, 0, 0])


@pytest.mark.parametrize("token", ["-0", "1e-400", "1.0000000000000001", "9007199254740992"])
def test_query_simulation_rejects_noncanonical_json_tokens(token):
    payload = (
        '{"sender":"alice","contract":"con_probe","function":"read","kwargs":{"nested":['
        + token
        + "]}}"
    )
    service = QuerySimulationService(client=None)
    result = asyncio.run(service.simulate_encoded_transaction(payload.encode().hex()))
    assert result["status"] == 1
    assert result["chi_used"] == 0


def test_the_estimate_is_rerun_until_its_digit_count_holds():
    # The chi used grows with the digits of chi_supplied in the envelope.
    supplied = []

    def run(chi_supplied):
        supplied.append(chi_supplied)
        return 9_998 + len(str(chi_supplied))

    assert estimate_to_fixpoint(run, lambda chi: chi) == 10_003
    assert supplied == [1, 9_999, 10_002]
    # A one-digit estimate needs no second run.
    assert estimate_to_fixpoint(lambda _chi: 7, lambda chi: chi) == 7


def test_the_estimated_envelope_carries_the_nonce_and_chi_supplied():
    size = _estimated_transaction_size(PAYLOAD, chain_id="x", nonce=0, chi_supplied=1)
    assert _estimated_transaction_size(PAYLOAD, chain_id="x", nonce=12, chi_supplied=1) == size + 1
    assert _estimated_transaction_size(PAYLOAD, chain_id="x", nonce=0, chi_supplied=999) == size + 2
    # A request's nonce is not counted twice.
    with_nonce = {**PAYLOAD, "nonce": 12}
    assert _estimated_transaction_size(with_nonce, chain_id="x", nonce=12, chi_supplied=1) == size + 1


def test_a_simulation_nonce_must_be_a_non_negative_integer():
    assert TransactionSimulator._normalize_payload({**PAYLOAD, "nonce": 4})["nonce"] == 4
    assert "nonce" not in TransactionSimulator._normalize_payload(PAYLOAD)
    for nonce in (-1, True, "3", 1.5):
        with pytest.raises(ValueError):
            TransactionSimulator._normalize_payload({**PAYLOAD, "nonce": nonce})


def test_without_a_nonce_the_sender_s_next_committed_nonce_sizes_the_envelope():
    driver = SimpleNamespace(get=lambda key: {"__n.alice:": 41}.get(key))
    simulator = _simulator(driver)
    assert simulator.envelope_nonce(PAYLOAD) == 42
    assert simulator.envelope_nonce({**PAYLOAD, "sender": "bob"}) == 0
    assert simulator.envelope_nonce({**PAYLOAD, "nonce": 3}) == 3
