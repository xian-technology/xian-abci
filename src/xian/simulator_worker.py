from __future__ import annotations

import sys
import traceback
from pathlib import Path

from contracting.local import ContractingClient
from contracting.storage.driver import Driver

from xian import simulator_ipc
from xian.execution_engine import VmRuntime, restore_driver_state
from xian.simulator import TransactionSimulator


def main() -> int:
    try:
        task = simulator_ipc.loads(sys.stdin.buffer.read())
        if not isinstance(task, dict):
            raise ValueError("simulation task must be a JSON object")
        storage_home = Path(task["storage_home"])
        driver = Driver(storage_home=storage_home)
        client = ContractingClient(
            storage_home=storage_home,
            driver=driver,
            submission_filename=None,
        )
        restore_driver_state(client.raw_driver, task.get("driver_state"))
        simulator = TransactionSimulator(
            client=client,
            execution_runtime=VmRuntime(runtime_info=task.get("runtime_info")),
            chain_id=task.get("chain_id"),
            charge_fees=task.get("charge_fees", True),
        )
        result = simulator.simulate(
            task["payload"],
            block_meta=task.get("block_meta"),
            max_chi=task["max_chi"],
        )
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    sys.stdout.buffer.write(simulator_ipc.dumps(result))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
