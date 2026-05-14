import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from xian.utils.block import (
    LATEST_BLOCK_DEFAULT,
    LATEST_BLOCK_READ_RETRY_DELAY_SECONDS,
    apply_state_changes_from_block,
    create_latest_block_json_if_not_exists,
    get_latest_block_hash,
    get_latest_block_height,
    get_latest_block_nanos,
    set_latest_block,
    set_latest_block_hash,
)


class LatestBlockUtilsTests(unittest.TestCase):
    def test_create_latest_block_json_initializes_defaults(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            create_latest_block_json_if_not_exists(storage_home)

            latest_block = json.loads(
                (storage_home / "__latest_block.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(latest_block, LATEST_BLOCK_DEFAULT)

    def test_set_latest_block_updates_all_fields(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            set_latest_block(
                block_hash=bytes.fromhex("ab" * 32),
                height=27,
                nanos=123456789,
                storage_home=storage_home,
            )

            latest_block = json.loads(
                (storage_home / "__latest_block.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(latest_block["hash"], "ab" * 32)
            self.assertEqual(
                get_latest_block_hash(storage_home),
                bytes.fromhex("ab" * 32),
            )
            self.assertEqual(get_latest_block_height(storage_home), 27)
            self.assertEqual(get_latest_block_nanos(storage_home), 123456789)

    def test_partial_update_preserves_other_latest_block_fields(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            set_latest_block(
                block_hash=bytes.fromhex("12" * 32),
                height=5,
                nanos=999,
                storage_home=storage_home,
            )
            set_latest_block_hash(bytes.fromhex("34" * 32), storage_home)
            self.assertEqual(
                get_latest_block_hash(storage_home),
                bytes.fromhex("34" * 32),
            )
            self.assertEqual(get_latest_block_height(storage_home), 5)
            self.assertEqual(get_latest_block_nanos(storage_home), 999)

    def test_read_retries_after_transient_partial_json(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)
            latest_block_path = storage_home / "__latest_block.json"
            latest_block_path.parent.mkdir(parents=True, exist_ok=True)
            latest_block_path.write_text('{"hash": ', encoding="utf-8")

            def repair_file() -> None:
                time.sleep(LATEST_BLOCK_READ_RETRY_DELAY_SECONDS * 2)
                latest_block_path.write_text(
                    json.dumps(
                        {
                            "hash": "cd" * 32,
                            "height": 42,
                            "nanos": 987654321,
                        }
                    ),
                    encoding="utf-8",
                )

            repair_thread = threading.Thread(target=repair_file)
            repair_thread.start()
            try:
                self.assertEqual(get_latest_block_height(storage_home), 42)
            finally:
                repair_thread.join()

    def test_apply_state_changes_does_not_materialize_runtime_artifacts(self):
        contract_source = """
@export
def ping():
    return 'pong'
"""

        class _RawDriver:
            def __init__(self):
                self.pending_writes = {}
                self.hard_apply_calls = []

            def set(self, key, value):
                self.pending_writes[key] = value

            def hard_apply(self, nanos):
                self.hard_apply_calls.append(nanos)

        class _NonceStorage:
            def __init__(self):
                self.nonces = {}

            def set_nonce(self, key, value):
                self.nonces[key] = value

        raw_driver = _RawDriver()
        nonce_storage = _NonceStorage()
        client = SimpleNamespace(raw_driver=raw_driver)
        block = {
            "genesis": [
                {"key": "con_test.__source__", "value": contract_source},
            ],
            "hlc_timestamp": 1234,
            "nonces": [],
            "rewards": [],
        }

        apply_state_changes_from_block(client, nonce_storage, block)

        self.assertEqual(
            raw_driver.pending_writes["con_test.__source__"],
            contract_source,
        )
        self.assertNotIn("con_test.__code__", raw_driver.pending_writes)
        self.assertNotIn("con_test.__xian_ir_v1__", raw_driver.pending_writes)
        self.assertEqual(raw_driver.hard_apply_calls, [1234])


if __name__ == "__main__":
    unittest.main()
