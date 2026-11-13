import unittest
from types import SimpleNamespace

from xian.xian_abci import Xian


def _budget(operator_cap, chain_max_tx_chi):
    app = SimpleNamespace(
        simulation_max_chi=operator_cap,
        tx_fee_policy=SimpleNamespace(max_tx_chi=chain_max_tx_chi),
    )
    return Xian.simulation_chi_budget(app)


class SimulationChiBudgetTests(unittest.TestCase):
    def test_without_operator_cap_the_chain_tx_cap_applies(self):
        self.assertEqual(_budget(None, 200_000), 200_000)

    def test_operator_cap_only_lowers_the_chain_tx_cap(self):
        self.assertEqual(_budget(50_000, 200_000), 50_000)
        self.assertEqual(_budget(900_000, 200_000), 200_000)


if __name__ == "__main__":
    unittest.main()
