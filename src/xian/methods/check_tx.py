import hashlib

from loguru import logger

from cometbft.abci.v1beta3.types_pb2 import ResponseCheckTx
from xian.app_logging import build_log_fields
from xian.constants import Constants as c
from xian.log_safety import error_fields
from xian.utils.tx import (
    decode_and_validate_transaction_static_bytes,
    validate_transaction_after_static,
)


def _rejected_check_tx(self, *, error: Exception, raw_tx: bytes) -> ResponseCheckTx:
    """Refuse a transaction; the refusal is counted, not logged one by one.

    Anyone can send a public node invalid transactions, so a rejection costs a
    counter update and a DEBUG record that does not re-encode the transaction;
    Commit reports the counts once per block (``report_check_tx_rejections``).
    """
    error_type = type(error).__name__
    self.check_tx_rejections[error_type] += 1
    logger.bind(
        **build_log_fields(stage="check_tx", raw_tx=raw_tx, extra=error_fields(error))
    ).debug("Rejected transaction during CheckTx")
    return ResponseCheckTx(code=c.ErrorCode, log=f"{error_type}: {error}")


def report_check_tx_rejections(self, *, height: int) -> None:
    """Log the CheckTx rejections since the last report as one record."""
    if not self.check_tx_rejections:
        return
    counts = dict(self.check_tx_rejections)
    self.check_tx_rejections.clear()
    logger.bind(
        **build_log_fields(
            stage="check_tx",
            block_height=height,
            extra={"rejected_txs": sum(counts.values()), **counts},
        )
    ).info("Rejected transactions during CheckTx")


async def check_tx(self, raw_tx) -> ResponseCheckTx:
    try:
        tx = decode_and_validate_transaction_static_bytes(
            raw_tx,
            chain_id=self.chain_id,
            max_raw_tx_bytes=self.max_tx_bytes,
        )
    except Exception as e:
        return _rejected_check_tx(self, error=e, raw_tx=raw_tx)

    try:
        tx_hash = hashlib.sha256(raw_tx).hexdigest()
        # Mempool validation leaves no trace in the pending block state.
        with self.client.raw_driver.discarding_changes():
            validate_transaction_after_static(
                self.client,
                self.nonce_storage,
                tx,
                tx_hash=tx_hash,
                fee_policy=self.tx_fee_policy,
            )
        return ResponseCheckTx(code=c.OkCode)
    except Exception as e:
        return _rejected_check_tx(self, error=e, raw_tx=raw_tx)
