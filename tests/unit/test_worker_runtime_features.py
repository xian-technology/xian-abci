"""Worker processes must judge runtime features exactly like the node."""

import io
import sys
from unittest import mock

from contracting.runtime_features import get_driver_runtime_features

from xian import simulator_ipc, simulator_worker
from xian.fee_policy import TxFeePolicy
from xian.simulator import QuerySimulationService


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
