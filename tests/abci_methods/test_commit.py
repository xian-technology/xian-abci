import logging
import unittest
from io import BytesIO
from unittest.mock import patch

from fixtures.mock_constants import MockConstants
from utils import setup_fixtures, teardown_fixtures

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestCommit
from cometbft.abci.v1beta3.types_pb2 import Request, Response
from xian.constants import Constants
from xian.utils.block import (
    get_latest_block_height,
    set_latest_block,
)
from xian.xian_abci import Xian

logging.disable(logging.CRITICAL)


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


class TestCommit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.app.current_block_meta = {"height": 0, "nanos": 0}
        self.app.merkle_root_hash = b"abc123"
        self.app.chain_id = "xian-testnet-1"
        self.app.current_block_rewards = {}
        self.handler = ProtocolHandler(self.app)

    async def asyncTearDown(self):
        teardown_fixtures()

    async def process_request(self, req):
        raw = await self.handler.process("commit", req)
        return await deserialize(raw)

    async def test_commit(self):
        self.app.nonce_storage.set_nonce("alice", 3)
        self.app.nonce_storage.set_pending_nonce("alice", 3)
        request = Request(commit=RequestCommit())

        with patch.object(self.app.client.raw_driver, "hard_apply") as hard_apply:
            response = await self.process_request(request)

        self.assertEqual(response.commit.retain_height, 0)
        hard_apply.assert_called_once_with("0")
        self.assertEqual(self.app.nonce_storage.pending_nonces, {})

    async def test_commit_preserves_future_pending_nonces(self):
        self.app.nonce_storage.set_nonce("alice", 3)
        self.app.nonce_storage.set_pending_nonce("alice", 5)
        request = Request(commit=RequestCommit())

        with patch.object(self.app.client.raw_driver, "hard_apply") as hard_apply:
            response = await self.process_request(request)

        self.assertEqual(response.commit.retain_height, 0)
        hard_apply.assert_called_once_with("0")
        self.assertEqual(self.app.nonce_storage.get_pending_nonce("alice"), 5)
        self.assertEqual(self.app.nonce_storage.get_next_nonce("alice"), 6)

    async def test_commit_persists_state_before_advancing_json_mirror(self):
        storage_home = self.app.client.raw_driver.storage_home
        set_latest_block(
            block_hash=bytes.fromhex("11" * 32),
            height=3,
            nanos=300,
            storage_home=storage_home,
        )
        self.app.current_block_meta = {"height": 4, "nanos": 400}
        self.app.merkle_root_hash = bytes.fromhex("22" * 32)
        original_hard_apply = self.app.client.raw_driver.hard_apply

        def assert_metadata_has_not_advanced(nanos):
            self.assertEqual(get_latest_block_height(storage_home), 3)
            return original_hard_apply(nanos)

        with patch.object(
            self.app.client.raw_driver,
            "hard_apply",
            side_effect=assert_metadata_has_not_advanced,
        ):
            await self.process_request(Request(commit=RequestCommit()))

        self.assertEqual(get_latest_block_height(storage_home), 4)
        self.assertEqual(
            self.app.client.raw_driver.value_from_disk(Constants.LATEST_BLOCK_KEY),
            {"hash": "22" * 32, "height": 4, "nanos": 400},
        )

    async def test_hard_apply_failure_keeps_old_metadata_and_retryable_writes(self):
        storage_home = self.app.client.raw_driver.storage_home
        set_latest_block(
            block_hash=bytes.fromhex("33" * 32),
            height=5,
            nanos=500,
            storage_home=storage_home,
        )
        self.app.current_block_meta = {"height": 6, "nanos": 600}
        self.app.merkle_root_hash = bytes.fromhex("44" * 32)
        self.app.client.raw_driver.apply_writes({"currency.balances:alice": 10})

        with (
            patch.object(
                self.app.client.raw_driver._store,
                "batch_set",
                side_effect=RuntimeError("injected persistence failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.process_request(Request(commit=RequestCommit()))

        self.assertEqual(get_latest_block_height(storage_home), 5)
        self.assertEqual(
            self.app.client.raw_driver.pending_writes["currency.balances:alice"],
            10,
        )
        self.assertEqual(
            self.app.client.raw_driver.pending_writes[Constants.LATEST_BLOCK_KEY],
            {"hash": "44" * 32, "height": 6, "nanos": 600},
        )
        self.assertIsNone(self.app.client.raw_driver.value_from_disk(Constants.LATEST_BLOCK_KEY))

        response = await self.process_request(Request(commit=RequestCommit()))
        self.assertEqual(response.commit.retain_height, 0)
        self.assertEqual(
            self.app.client.raw_driver.value_from_disk("currency.balances:alice"),
            10,
        )
        self.assertEqual(
            self.app.client.raw_driver.value_from_disk(Constants.LATEST_BLOCK_KEY),
            {"hash": "44" * 32, "height": 6, "nanos": 600},
        )

    async def test_json_mirror_failure_does_not_fail_durable_commit(self):
        storage_home = self.app.client.raw_driver.storage_home
        set_latest_block(
            block_hash=bytes.fromhex("55" * 32),
            height=7,
            nanos=700,
            storage_home=storage_home,
        )
        self.app.current_block_meta = {"height": 8, "nanos": 800}
        self.app.merkle_root_hash = bytes.fromhex("66" * 32)
        self.app.client.raw_driver.apply_writes({"currency.balances:alice": 20})

        with (
            patch(
                "xian.utils.block._write_latest_block_json",
                side_effect=OSError("injected mirror failure"),
            ),
            patch("xian.utils.block.logger") as mirror_logger,
        ):
            response = await self.process_request(Request(commit=RequestCommit()))

        self.assertEqual(response.commit.retain_height, 0)
        self.assertEqual(get_latest_block_height(storage_home), 7)
        self.assertEqual(
            self.app.client.raw_driver.value_from_disk("currency.balances:alice"),
            20,
        )
        self.assertEqual(
            self.app.client.raw_driver.value_from_disk(Constants.LATEST_BLOCK_KEY),
            {"hash": "66" * 32, "height": 8, "nanos": 800},
        )
        mirror_logger.bind.return_value.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
