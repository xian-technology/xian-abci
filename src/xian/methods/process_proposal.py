from loguru import logger

from cometbft.abci.v1beta2.types_pb2 import ResponseProcessProposal
from xian.app_logging import build_log_fields
from xian.utils.encoding import decode_transaction_bytes
from xian.utils.tx import (
    SequentialNonceTracker,
    validate_consensus_transaction,
)


async def process_proposal(self, req) -> ResponseProcessProposal:
    response = ResponseProcessProposal()
    nonce_tracker = SequentialNonceTracker(self.nonce_storage.get_nonce)

    for raw_tx in req.txs:
        tx = None
        try:
            tx, _ = decode_transaction_bytes(raw_tx)
            validate_consensus_transaction(
                tx,
                chain_id=self.chain_id,
                nonce_tracker=nonce_tracker,
            )
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="process_proposal",
                    tx=tx,
                    raw_tx=raw_tx,
                    block_height=getattr(req, "height", None),
                )
            ).warning("Rejecting invalid proposal transaction: {}", exc)
            response.status = ResponseProcessProposal.ProposalStatus.REJECT
            return response

    response.status = ResponseProcessProposal.ProposalStatus.ACCEPT
    return response
