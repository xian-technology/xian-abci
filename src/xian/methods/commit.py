from cometbft.abci.v1beta3.types_pb2 import ResponseCommit
from xian.utils.block import stage_latest_block, try_write_latest_block


async def commit(self) -> ResponseCommit:
    self._restore_pending_commit_driver_state()

    latest_block = stage_latest_block(
        self.client.raw_driver,
        block_hash=self.merkle_root_hash,
        height=self.current_block_meta["height"],
        nanos=self.current_block_meta["nanos"],
    )

    # One atomic LMDB write; on failure the finalized block writes stay pending,
    # so the commit can be retried.
    self.client.raw_driver.commit()
    self.state_root_cache.commit()
    self.nonce_storage.reconcile_pending()
    # The next block runs under the fee policy and runtime features this block committed.
    self.refresh_tx_fee_policy()
    self.refresh_runtime_features()

    # This file remains for compatibility with offline tools. The LMDB marker
    # above is authoritative and startup/Info reconcile this mirror after a
    # crash between the two persistence steps.
    try_write_latest_block(latest_block, self.client.raw_driver.storage_home)

    # unset current_block_meta & cleanup
    self.merkle_root_hash = None
    self.current_block_rewards = {}

    retain_height = 0
    if self.pruning_enabled:
        if self.current_block_meta["height"] > self.blocks_to_keep:
            retain_height = self.current_block_meta["height"] - self.blocks_to_keep

    self.current_block_meta = None

    return ResponseCommit(retain_height=retain_height)
