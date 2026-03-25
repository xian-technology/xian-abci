import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from xian.simulator import TransactionSimulator
from xian.utils.block import set_latest_block_nanos


class SimulatorTests(unittest.TestCase):
    def test_make_environment_uses_latest_committed_block_time_when_idle(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)
            expected_nanos = 1_710_000_000_123_000_000
            set_latest_block_nanos(expected_nanos, storage_home)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
            simulator.get_block_meta = lambda: None

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 2024)
            self.assertEqual(environment["now"].month, 3)
            self.assertEqual(environment["now"].day, 9)
            self.assertEqual(environment["now"].microsecond, 123000)

    def test_make_environment_falls_back_to_epoch_without_chain_time(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
            simulator.get_block_meta = lambda: None

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 1970)
            self.assertEqual(environment["now"].month, 1)
            self.assertEqual(environment["now"].day, 1)

    def test_make_environment_is_deterministic_for_same_payload(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
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
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
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


if __name__ == "__main__":
    unittest.main()
