from loguru import logger

from cometbft.abci.v1beta2.types_pb2 import ResponsePrepareProposal
from xian.app_logging import build_log_fields
from xian.utils.tx import (
    SequentialNonceTracker,
    decode_and_validate_transaction_static_bytes,
    validate_consensus_transaction_after_static,
)


async def prepare_proposal(self, req) -> ResponsePrepareProposal:
    nonce_tracker = SequentialNonceTracker(self.nonce_storage.get_nonce)
    txs = []

    for raw_tx in req.txs:
        tx = None
        try:
            tx = decode_and_validate_transaction_static_bytes(
                raw_tx,
                chain_id=self.chain_id,
            )
            validate_consensus_transaction_after_static(
                tx,
                nonce_tracker=nonce_tracker,
            )
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="prepare_proposal",
                    tx=tx,
                    raw_tx=raw_tx,
                    block_height=getattr(req, "height", None),
                )
            ).warning(
                "Dropping invalid transaction from proposal preparation: {}",
                exc,
            )
            continue
        txs.append(raw_tx)

    response = ResponsePrepareProposal(txs=txs)
    return response
