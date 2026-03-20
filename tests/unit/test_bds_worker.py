import asyncio
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig
from xian.services.bds.payloads import BdsBlockPayload


def _payload(height: int, block_hash: str) -> BdsBlockPayload:
    return BdsBlockPayload(
        block_meta={"height": height, "hash": block_hash, "nanos": height},
        block_time=datetime(2026, 1, height, tzinfo=UTC),
        app_hash=f"APP-{height}",
    )


class _RecordingBDS(BDS):
    def __init__(self, spool_dir: str):
        super().__init__(BdsConfig(queue_max_size=4, spool_dir=spool_dir))
        self.persisted_heights = []

    async def persist_block(self, payload: BdsBlockPayload) -> bool:
        self.persisted_heights.append(payload.block_meta["height"])
        return True


class BdsWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_persists_blocks_in_order(self):
        with TemporaryDirectory() as spool_dir:
            bds = _RecordingBDS(spool_dir)
            Path(spool_dir).mkdir(parents=True, exist_ok=True)
            bds._start_worker()

            await bds.enqueue_block(_payload(7, "A"))
            await bds.enqueue_block(_payload(8, "B"))

            await bds.flush()
            await bds.close()

            self.assertEqual(bds.persisted_heights, [7, 8])
            self.assertEqual(list(Path(spool_dir).glob("*.json")), [])

    async def test_replay_spool_enqueues_pending_blocks_in_order(self):
        with TemporaryDirectory() as spool_dir:
            bds = _RecordingBDS(spool_dir)
            Path(spool_dir).mkdir(parents=True, exist_ok=True)
            bds._queue = asyncio.Queue()

            first = _payload(11, "A")
            second = _payload(12, "B")
            bds._write_spool_file(second)
            bds._write_spool_file(first)

            await bds._replay_spool()

            queued_first = await bds._queue.get()
            queued_second = await bds._queue.get()
            self.assertEqual(queued_first[0].block_meta["height"], 11)
            self.assertEqual(queued_second[0].block_meta["height"], 12)
            self.assertTrue(queued_first[1].name.endswith("-A.json"))
            self.assertTrue(queued_second[1].name.endswith("-B.json"))

    async def test_status_reports_spool_and_index_lag(self):
        with TemporaryDirectory() as spool_dir:
            bds = _RecordingBDS(spool_dir)
            Path(spool_dir).mkdir(parents=True, exist_ok=True)
            bds._write_spool_file(_payload(11, "A"))
            bds._write_spool_file(_payload(12, "B"))
            bds.db.fetchrow = AsyncMock(
                return_value={
                    "indexed_block_count": 10,
                    "indexed_height": 9,
                    "indexed_block_hash": "BLOCK-9",
                    "indexed_block_time": 9,
                    "indexed_block_time_iso": datetime(2026, 1, 1, tzinfo=UTC),
                    "indexed_tx_count": 3,
                    "indexed_app_hash": "APP-9",
                }
            )

            status = await bds.get_status(current_block_height=12)

            self.assertFalse(status["worker_running"])
            self.assertEqual(status["spool_pending_count"], 2)
            self.assertGreater(status["spool_total_bytes"], 0)
            self.assertEqual(status["spool_oldest_pending"]["block_height"], 11)
            self.assertEqual(status["spool_newest_pending"]["block_height"], 12)
            self.assertEqual(status["indexed"]["indexed_height"], 9)
            self.assertEqual(status["height_lag"], 3)
            self.assertTrue(status["catching_up"])
            self.assertIsInstance(status["alerts"], list)

    async def test_spool_entries_return_ordered_metadata(self):
        with TemporaryDirectory() as spool_dir:
            bds = _RecordingBDS(spool_dir)
            Path(spool_dir).mkdir(parents=True, exist_ok=True)
            bds._write_spool_file(_payload(12, "B"))
            bds._write_spool_file(_payload(11, "A"))

            spool_entries = await bds.get_spool_entries(limit=10, offset=0)

            self.assertEqual(
                [entry["block_height"] for entry in spool_entries], [11, 12]
            )
            self.assertEqual(spool_entries[0]["block_hash"], "A")
            self.assertEqual(spool_entries[1]["block_hash"], "B")


if __name__ == "__main__":
    unittest.main()
