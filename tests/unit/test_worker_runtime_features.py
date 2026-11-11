"""Worker processes must judge runtime features exactly like the node."""

import io
import sys
from unittest import mock

from contracting.runtime_features import get_driver_runtime_features

from xian import parallel_executor, simulator_ipc, simulator_worker
from xian.fee_policy import TxFeePolicy
from xian.simulator import QuerySimulationService


def test_parallel_worker_installs_node_runtime_features(tmp_path):
    executor = parallel_executor.ParallelBlockExecutor(
        storage_home=tmp_path,
        enabled=True,
        workers=1,
        runtime_features={"zk": False},
    )
    task = executor._speculation_task(
        {"payload": {}}, fee_policy=TxFeePolicy.system_unmetered(), base_pending_writes={}
    )
    with mock.patch.object(parallel_executor.TxProcessor, "process_tx", return_value={}):
        parallel_executor._speculative_process_tx(task)
    runtime = parallel_executor._get_worker_runtime(
        storage_home=str(tmp_path), use_rewards_handler=False
    )
    try:
        assert get_driver_runtime_features(runtime.client.raw_driver) == {"zk": False}
    finally:
        runtime.client.raw_driver.close()
        parallel_executor._WORKER_RUNTIMES.clear()


def test_simulation_worker_installs_node_runtime_features(tmp_path, monkeypatch):
    service = QuerySimulationService(storage_home=tmp_path, runtime_features={"zk": False})
    prepared = service.prepare_encoded_transaction(
        b'{"sender":"alice","contract":"con_x","function":"f","kwargs":{}}'.hex()
    )
    observed = {}

    def fake_simulate(self, payload, **_kwargs):
        observed["features"] = get_driver_runtime_features(self.client.raw_driver)
        return {"status": 0}

    monkeypatch.setattr(simulator_worker.TransactionSimulator, "simulate", fake_simulate)
    monkeypatch.setattr(
        sys, "stdin", mock.Mock(buffer=io.BytesIO(simulator_ipc.dumps(prepared.task)))
    )
    monkeypatch.setattr(sys, "stdout", mock.Mock(buffer=io.BytesIO()))
    assert simulator_worker.main() == 0
    assert observed["features"] == {"zk": False}
