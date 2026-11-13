"""Simulation capacity is released when a simulation finishes."""

import unittest

from test_simulator import _TestQuerySimulationService

PAYLOAD = (
    b'{"sender":"alice","contract":"currency","function":"balance_of","kwargs":{"account":"alice"}}'
).hex()


class SimulationCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_simulations_free_their_slot(self):
        service = _TestQuerySimulationService(enabled=True, max_concurrency=1, timeout_ms=1000)
        service.release_event.set()

        results = [await service.simulate_encoded_transaction(PAYLOAD) for _ in range(3)]

        self.assertEqual([result["status"] for result in results], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
