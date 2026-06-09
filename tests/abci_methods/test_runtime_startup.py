import unittest
from unittest.mock import AsyncMock, Mock, patch

from fixtures.mock_constants import MockConstants
from utils import setup_fixtures, teardown_fixtures

from xian.xian_abci import (
    Xian,
    _require_zk_verifier_if_enabled,
    resolve_abci_socket_path,
)


class AbciSocketPathTests(unittest.TestCase):
    def test_resolve_abci_socket_path_defaults_and_normalizes_unix_proxy(self):
        self.assertEqual(resolve_abci_socket_path(None), "/tmp/abci.sock")
        self.assertEqual(
            resolve_abci_socket_path("unix:///tmp/custom-abci.sock"),
            "/tmp/custom-abci.sock",
        )

    def test_resolve_abci_socket_path_rejects_non_unix_proxy(self):
        with self.assertRaisesRegex(ValueError, "only supports unix"):
            resolve_abci_socket_path("tcp://127.0.0.1:26658")

    def test_zk_verifier_gate_noops_when_feature_disabled(self):
        is_available = Mock(side_effect=AssertionError("should not be called"))
        with patch("contracting.stdlib.bridge.zk.is_available", is_available):
            _require_zk_verifier_if_enabled({"zk": False})

    def test_zk_verifier_gate_exits_when_enabled_and_unavailable(self):
        with patch("contracting.stdlib.bridge.zk.is_available", return_value=False):
            with self.assertRaises(SystemExit):
                _require_zk_verifier_if_enabled({"zk": True})


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

        self.app.bds_enabled = True
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

    async def test_runtime_disables_debug_tx_traces_when_log_level_is_info(
        self,
    ):
        with (
            patch(
                "xian.xian_abci.load_tendermint_config",
                return_value={
                    "moniker": "validator-1",
                },
            ),
            patch(
                "xian.xian_abci.load_xian_config",
                return_value={
                    "transaction_trace_logging": True,
                    "app_log_level": "INFO",
                },
            ),
            patch(
                "xian.xian_abci.load_genesis_data",
                return_value={
                    "chain_id": "xian-testnet-1",
                    "abci_genesis": {},
                },
            ),
        ):
            app = Xian(constants=MockConstants)

        self.assertTrue(app.transaction_trace_logging)
        self.assertFalse(app.transaction_trace_debug_logging)
        self.assertFalse(app.transaction_trace_full_logging)
        self.assertFalse(app.tx_processor.trace_logging)
        await app.close()

    async def test_runtime_enables_full_tx_traces_only_at_trace_level(self):
        with (
            patch(
                "xian.xian_abci.load_tendermint_config",
                return_value={
                    "moniker": "validator-1",
                },
            ),
            patch(
                "xian.xian_abci.load_xian_config",
                return_value={
                    "transaction_trace_logging": True,
                    "app_log_level": "TRACE",
                },
            ),
            patch(
                "xian.xian_abci.load_genesis_data",
                return_value={
                    "chain_id": "xian-testnet-1",
                    "abci_genesis": {},
                },
            ),
        ):
            app = Xian(constants=MockConstants)

        self.assertTrue(app.transaction_trace_logging)
        self.assertTrue(app.transaction_trace_debug_logging)
        self.assertTrue(app.transaction_trace_full_logging)
        self.assertTrue(app.tx_processor.trace_logging)
        await app.close()


if __name__ == "__main__":
    unittest.main()
