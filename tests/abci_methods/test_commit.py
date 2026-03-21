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
        self.app.fingerprint_hashes = []
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


if __name__ == "__main__":
    unittest.main()
