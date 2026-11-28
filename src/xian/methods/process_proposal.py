from loguru import logger

from tendermint.abci.types_pb2 import ResponseProcessProposal
from xian.account_registry import registry_for_app
from xian.app_logging import build_log_fields
from xian.log_safety import error_fields
from xian.nonce import BlockNonceTracker
from xian.utils.tx import (
    decode_and_validate_transaction_static_bytes,
)


async def process_proposal(self, req) -> ResponseProcessProposal:
    response = ResponseProcessProposal()
    account_registry = registry_for_app(self)
    nonce_tracker = BlockNonceTracker(self.nonce_storage.get_nonce)
    block_chi_supplied = 0

    for raw_tx in req.txs:
        tx = None
        try:
            tx = decode_and_validate_transaction_static_bytes(
                raw_tx,
                chain_id=self.chain_id,
                admission_cache=self.transaction_admission_cache,
                account_registry=account_registry,
            )
            self.tx_fee_policy.validate_tx(tx)
            next_block_chi_supplied = block_chi_supplied + self.tx_fee_policy.tx_chi_supplied(tx)
            self.tx_fee_policy.validate_block_total(next_block_chi_supplied)
            nonce_tracker.validate_and_advance(tx)
            account_registry.accept(tx)
            block_chi_supplied = next_block_chi_supplied
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="process_proposal",
                    tx=tx,
                    raw_tx=raw_tx,
                    block_height=getattr(req, "height", None),
                    extra=error_fields(exc),
                )
            ).warning("Rejecting invalid proposal transaction")
            response.status = ResponseProcessProposal.ProposalStatus.REJECT
            return response

    response.status = ResponseProcessProposal.ProposalStatus.ACCEPT
    return response
