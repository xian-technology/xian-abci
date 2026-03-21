from loguru import logger

from cometbft.abci.v1beta2.types_pb2 import ResponsePrepareProposal
from xian.utils.encoding import decode_transaction_bytes
from xian.utils.tx import (
    SequentialNonceTracker,
    validate_consensus_transaction,
)


async def prepare_proposal(self, req) -> ResponsePrepareProposal:
    nonce_tracker = SequentialNonceTracker(self.nonce_storage.get_nonce)
    txs = []

    for raw_tx in req.txs:
        try:
            tx, _ = decode_transaction_bytes(raw_tx)
            validate_consensus_transaction(
                tx,
                chain_id=self.chain_id,
                nonce_tracker=nonce_tracker,
            )
        except Exception as exc:
            logger.warning(
                "Dropping invalid transaction from proposal preparation: {}",
                exc,
            )
            continue
        txs.append(raw_tx)

    response = ResponsePrepareProposal(txs=txs)
    return response
