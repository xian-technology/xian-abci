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

    # `hard_apply` clears these containers before it opens the LMDB write. Keep
    # shallow backups so a failed transaction remains retryable without deep-
    # copying every value in a potentially large block.
    pending_writes = self.client.raw_driver.pending_writes.copy()
    pending_reads = self.client.raw_driver.pending_reads.copy()
    try:
        self.client.raw_driver.hard_apply(str(self.current_block_meta["nanos"]))
    except Exception:
        # LMDB commits atomically. Restore the in-memory batch so a transient
        # failure can be retried without losing the finalized block writes.
        self.client.raw_driver.rollback(str(self.current_block_meta["nanos"]))
        self.client.raw_driver.pending_writes = pending_writes
        self.client.raw_driver.pending_reads = pending_reads
        raise
    self.state_root_cache.commit()
    self.nonce_storage.reconcile_pending()

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
