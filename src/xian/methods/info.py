from cometbft.abci.v1beta1.types_pb2 import ResponseInfo
from xian.utils.block import reconcile_latest_block


async def info(self, req) -> ResponseInfo:
    res = ResponseInfo()
    res.app_version = self.app_version
    res.version = req.version
    latest_block = reconcile_latest_block(
        self.client.raw_driver,
        self.client.raw_driver.storage_home,
    )
    res.last_block_height = latest_block["height"]
    res.last_block_app_hash = bytes.fromhex(latest_block["hash"])
    return res
