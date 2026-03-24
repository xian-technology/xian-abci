import unittest
from unittest.mock import AsyncMock

from fixtures.mock_constants import MockConstants
from utils import setup_fixtures, teardown_fixtures

from xian.xian_abci import Xian


class RuntimeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)

    async def asyncTearDown(self):
        await self.app.close()
        teardown_fixtures()

    async def test_start_runtime_initializes_bds_on_runtime_loop(self):
        fake_bds = type(
            "FakeBDS",
            (),
            {
                "initialize_storage": AsyncMock(),
                "start": AsyncMock(),
                "close": AsyncMock(),
            },
        )()
        fake_metrics = type(
            "FakeMetrics",
            (),
            {
                "start": AsyncMock(),
                "close": AsyncMock(),
            },
        )()

        self.app.block_service_mode = True
        self.app.bds = fake_bds
        self.app.metrics_service = fake_metrics
        self.app._bds_storage_initialized = False

        await self.app.start_runtime()

        fake_bds.initialize_storage.assert_awaited_once_with(
            cometbft_genesis=self.app.genesis
        )
        fake_bds.start.assert_awaited_once()
        fake_metrics.start.assert_awaited_once()
        self.assertTrue(self.app._bds_storage_initialized)


if __name__ == "__main__":
    unittest.main()
