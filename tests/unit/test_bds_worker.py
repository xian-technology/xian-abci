import unittest
from datetime import UTC, datetime

from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig
from xian.services.bds.payloads import BdsBlockPayload


class _RecordingBDS(BDS):
    def __init__(self):
        super().__init__(BdsConfig(queue_max_size=2))
        self.persisted_heights = []

    async def persist_block(self, payload: BdsBlockPayload) -> None:
        self.persisted_heights.append(payload.block_meta["height"])


class BdsWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_persists_blocks_in_order(self):
        bds = _RecordingBDS()
        bds._start_worker()

        await bds.enqueue_block(
            BdsBlockPayload(
                block_meta={"height": 7, "hash": "A", "nanos": 1},
                block_time=datetime(2026, 1, 1, tzinfo=UTC),
                app_hash="APP-7",
            )
        )
        await bds.enqueue_block(
            BdsBlockPayload(
                block_meta={"height": 8, "hash": "B", "nanos": 2},
                block_time=datetime(2026, 1, 2, tzinfo=UTC),
                app_hash="APP-8",
            )
        )

        await bds.flush()
        await bds.close()

        self.assertEqual(bds.persisted_heights, [7, 8])


if __name__ == "__main__":
    unittest.main()
