from cometbft.abci.v1beta1.types_pb2 import ResponseOfferSnapshot
from xian.utils.block import get_committed_latest_block


async def offer_snapshot(self, req) -> ResponseOfferSnapshot:
    """Unwrap a paired local offer before normal snapshot validation."""
    try:
        metadata = self._evidence_history_pairing.unwrap_offer(
            req.snapshot.metadata,
            maximum=self.state_snapshot_manager.limits.max_metadata_bytes,
        )
    except (ValueError, TypeError):
        # Capability values and wrapped bytes must never enter logs/responses.
        return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
    snapshot = type(req.snapshot)()
    snapshot.CopyFrom(req.snapshot)
    snapshot.metadata = metadata
    return self.state_snapshot_manager.offer_snapshot_response(
        snapshot,
        app_hash=req.app_hash,
        current_height=get_committed_latest_block(self.client.raw_driver)["height"],
    )
