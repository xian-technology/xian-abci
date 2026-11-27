from collections import Counter

from loguru import logger

from tendermint.abci.types_pb2 import ResponsePrepareProposal
from xian.app_logging import build_log_fields
from xian.exceptions import TransactionException
from xian.nonce import BlockNonceTracker
from xian.utils.tx import (
    decode_and_validate_transaction_static_bytes,
)


def _admit(
    self, raw_tx: bytes, block_chi_supplied: int, chi_remaining: int | None, nonce_tracker
) -> tuple[str, int]:
    """Classify one mempool transaction for this proposal: ``included`` or a skip reason.

    Skipped transactions cost no log record each: under load the mempool holds
    many blocks' worth of transactions, and a record per skip made proposal
    preparation outlast the propose timeout.
    """
    try:
        tx = decode_and_validate_transaction_static_bytes(
            raw_tx,
            chain_id=self.chain_id,
            max_raw_tx_bytes=self.max_tx_bytes,
            admission_cache=self.transaction_admission_cache,
            proposal_chi_remaining=chi_remaining,
        )
        if tx is not None:
            self.tx_fee_policy.validate_tx(tx)
    except Exception:
        return "skipped_invalid", block_chi_supplied
    if tx is None:
        return "skipped_over_block_chi", block_chi_supplied
    next_block_chi_supplied = block_chi_supplied + self.tx_fee_policy.tx_chi_supplied(tx)
    if not self.tx_fee_policy.fits_block(next_block_chi_supplied):
        # Keep looking for a smaller transaction, as for the byte budget.
        return "skipped_over_block_chi", block_chi_supplied
    try:
        nonce_tracker.validate_and_advance(tx)
    except TransactionException:
        return "skipped_nonce", block_chi_supplied
    return "included_txs", next_block_chi_supplied


async def prepare_proposal(self, req) -> ResponsePrepareProposal:
    nonce_tracker = BlockNonceTracker(self.nonce_storage.get_nonce)
    txs = []
    counts = Counter()
    block_chi_supplied = 0
    block_tx_bytes = 0
    chi_limit = self.tx_fee_policy.max_block_chi if self.tx_fee_policy.meter_execution else None

    for raw_tx in req.txs:
        # A valid wire transaction takes bytes and, on metered chains, positive chi.
        if block_tx_bytes == req.max_tx_bytes or block_chi_supplied == chi_limit:
            break
        next_block_tx_bytes = block_tx_bytes + len(raw_tx)
        if next_block_tx_bytes > req.max_tx_bytes:
            # Keep looking for a smaller transaction without advancing nonce
            # or chi accounting for a transaction that cannot be included.
            counts["skipped_over_block_bytes"] += 1
            continue
        chi_remaining = None if chi_limit is None else chi_limit - block_chi_supplied
        outcome, block_chi_supplied = _admit(
            self, raw_tx, block_chi_supplied, chi_remaining, nonce_tracker
        )
        counts[outcome] += 1
        if outcome == "included_txs":
            txs.append(raw_tx)
            block_tx_bytes = next_block_tx_bytes

    if len(txs) != len(req.txs):
        counts["skipped_at_capacity"] = len(req.txs) - counts.total()
        logger.bind(
            **build_log_fields(
                stage="prepare_proposal",
                block_height=getattr(req, "height", None),
                extra={**counts, "included_txs": len(txs)},
            )
        ).info("Skipped mempool transactions during proposal preparation")
    return ResponsePrepareProposal(txs=txs)
