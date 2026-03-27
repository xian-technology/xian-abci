import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xian.app_logging import build_log_fields, default_logs_directory


class AppLoggingTests(unittest.TestCase):
    def test_default_logs_directory_uses_storage_home(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            constants = SimpleNamespace(STORAGE_HOME=Path(tmp_dir) / "xian")
            self.assertEqual(
                default_logs_directory(constants),
                Path(tmp_dir) / "xian" / "logs",
            )

    def test_build_log_fields_includes_tx_and_raw_context(self):
        tx = {
            "payload": {
                "sender": "alice",
                "contract": "currency",
                "function": "transfer",
                "nonce": 7,
                "kwargs": {"amount": 1, "to": "bob"},
                "stamps_supplied": 100,
            },
            "metadata": {"signature": "00" * 64},
        }

        fields = build_log_fields(
            stage="check_tx",
            tx=tx,
            raw_tx=b'{"payload":"example"}',
            block_height=9,
            status=1,
            extra={"reason": "Bad signature"},
        )

        self.assertEqual(fields["stage"], "check_tx")
        self.assertEqual(fields["block_height"], 9)
        self.assertEqual(fields["sender"], "alice")
        self.assertEqual(fields["contract"], "currency")
        self.assertEqual(fields["function"], "transfer")
        self.assertEqual(fields["nonce"], 7)
        self.assertEqual(fields["status"], 1)
        self.assertIn("raw_tx_hash", fields)
        self.assertIn("tx_hash", fields)
        self.assertIn("reason=Bad signature", fields["context"])
        self.assertIn("sender=alice", fields["context"])


if __name__ == "__main__":
    unittest.main()
