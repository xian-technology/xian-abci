import hashlib

from loguru import logger

from cometbft.abci.v1beta3.types_pb2 import ResponseCheckTx
from xian.app_logging import build_log_fields
from xian.constants import Constants as c
from xian.utils.encoding import decode_transaction_bytes
from xian.utils.tx import validate_transaction


async def check_tx(self, raw_tx) -> ResponseCheckTx:
    try:
        tx, _ = decode_transaction_bytes(raw_tx)
    except Exception as e:
        logger.bind(
            **build_log_fields(stage="check_tx", raw_tx=raw_tx)
        ).warning("Rejected transaction during CheckTx: {}", e)
        return ResponseCheckTx(code=c.ErrorCode, log=f"{type(e).__name__}: {e}")

    try:
        tx_hash = hashlib.sha256(raw_tx).hexdigest()
        validate_transaction(
            self.client,
            self.nonce_storage,
            tx,
            tx_hash=tx_hash,
            chain_id=self.chain_id,
        )
        return ResponseCheckTx(code=c.OkCode)
    except Exception as e:
        logger.bind(
            **build_log_fields(stage="check_tx", tx=tx, raw_tx=raw_tx)
        ).warning("Rejected transaction during CheckTx: {}", e)
        return ResponseCheckTx(code=c.ErrorCode, log=f"{type(e).__name__}: {e}")
