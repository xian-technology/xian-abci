import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from xian.exceptions import TransactionException
from xian.nonce import NonceStorage


class _RawDriver:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def flush_file(self, filename):
        prefix = f"{filename}."
        for key in list(self.values):
            if key.startswith(prefix):
                del self.values[key]


class _Client:
    def __init__(self):
        self.raw_driver = _RawDriver()


class _SlowCommittedNonceStorage(NonceStorage):
    def _get_committed_nonce(self, sender: str) -> int | None:
        time.sleep(0.02)
        return super()._get_committed_nonce(sender)


class TestNonceStorage(unittest.TestCase):
    def test_check_nonce_reservation_is_thread_safe(self):
        storage = _SlowCommittedNonceStorage(_Client())
        tx = {"payload": {"sender": "alice", "nonce": 0}}

        def reserve(tx_hash):
            try:
                storage.check_nonce(tx, tx_hash=tx_hash)
            except TransactionException:
                return False
            return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(reserve, [f"different-hash-{i}" for i in range(8)])
            )

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)
        self.assertEqual(storage.get_pending_nonce("alice"), 0)


if __name__ == "__main__":
    unittest.main()
