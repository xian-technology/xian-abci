from __future__ import annotations

import pickle
import sys
import traceback
from pathlib import Path

from contracting.client import ContractingClient
from contracting.storage.driver import Driver

from xian.execution_engine import restore_driver_state
from xian.simulator import TransactionSimulator


def main() -> int:
    try:
        task = pickle.load(sys.stdin.buffer)
        storage_home = Path(task["storage_home"])
        driver = Driver(storage_home=storage_home)
        client = ContractingClient(
            storage_home=storage_home,
            driver=driver,
            submission_filename=None,
            tracer_mode=task["tracer_mode"],
        )
        restore_driver_state(client.raw_driver, task.get("driver_state"))
        simulator = TransactionSimulator(
            client=client,
            execution_runtime=task.get("execution_runtime"),
        )
        result = simulator.simulate(
            task["payload"],
            block_meta=task.get("block_meta"),
            max_chi=task["max_chi"],
        )
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    pickle.dump(result, sys.stdout.buffer)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
