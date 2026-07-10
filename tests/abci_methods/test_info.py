import logging
import unittest
from io import BytesIO
from unittest.mock import patch

from fixtures.mock_constants import MockConstants
from utils import setup_fixtures, teardown_fixtures

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestCommit
from cometbft.abci.v1beta2.types_pb2 import (
    RequestInfo,
)
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    Response,
)
from xian.utils.block import (
    get_latest_block_height,
    set_latest_block,
    stage_latest_block,
)
from xian.xian_abci import Xian

# Disable any kind of logging
logging.disable(logging.CRITICAL)


async def deserialize(raw: bytes) -> Response:
    try:
        resp = next(read_messages(BytesIO(raw), Response))
        return resp
    except Exception as e:
        logging.error("Deserialization error: %s", e)
        raise


class TestInfo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.app.current_block_meta = {"height": 0, "nanos": 0}
        self.handler = ProtocolHandler(self.app)
        self.app.merkle_root_hash = b""

    async def asyncTearDown(self):
        teardown_fixtures()

    async def process_request(self, request_type, req):
        raw = await self.handler.process(request_type, req)
        resp = await deserialize(raw)
        return resp

    async def test_info(self):
        commit_request = Request(commit=RequestCommit())
        await self.process_request("commit", commit_request)
        request = Request(info=RequestInfo())
        response = await self.process_request("info", request)
        self.assertEqual(response.info.app_version, 1)
        self.assertEqual(response.info.data, "")  # We don't use that
        self.assertEqual(response.info.version, "")  # Not running CometBFT
        self.assertEqual(response.info.last_block_height, 0)
        self.assertEqual(response.info.last_block_app_hash, b"")

    async def test_info_reconciles_crash_after_state_commit(self):
        storage_home = self.app.client.raw_driver.storage_home
        set_latest_block(
            block_hash=bytes.fromhex("55" * 32),
            height=9,
            nanos=900,
            storage_home=storage_home,
        )
        stage_latest_block(
            self.app.client.raw_driver,
            block_hash=bytes.fromhex("66" * 32),
            height=10,
            nanos=1000,
        )
        self.app.client.raw_driver.hard_apply("1000")

        self.assertEqual(get_latest_block_height(storage_home), 9)
        response = await self.process_request("info", Request(info=RequestInfo()))

        self.assertEqual(response.info.last_block_height, 10)
        self.assertEqual(
            response.info.last_block_app_hash,
            bytes.fromhex("66" * 32),
        )
        self.assertEqual(get_latest_block_height(storage_home), 10)

    async def test_info_uses_authoritative_marker_when_mirror_repair_fails(self):
        storage_home = self.app.client.raw_driver.storage_home
        set_latest_block(
            block_hash=bytes.fromhex("77" * 32),
            height=11,
            nanos=1100,
            storage_home=storage_home,
        )
        stage_latest_block(
            self.app.client.raw_driver,
            block_hash=bytes.fromhex("88" * 32),
            height=12,
            nanos=1200,
        )
        self.app.client.raw_driver.hard_apply("1200")

        with (
            patch(
                "xian.utils.block._write_latest_block_json",
                side_effect=OSError("injected mirror repair failure"),
            ),
            patch("xian.utils.block.logger") as mirror_logger,
        ):
            response = await self.process_request("info", Request(info=RequestInfo()))

        self.assertEqual(response.info.last_block_height, 12)
        self.assertEqual(
            response.info.last_block_app_hash,
            bytes.fromhex("88" * 32),
        )
        self.assertEqual(get_latest_block_height(storage_home), 11)
        mirror_logger.bind.return_value.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
