from loguru import logger

from tendermint.abci.types_pb2 import ResponseCheckTx
from xian.account_registry import ACCOUNT_PREFIXES, COMPACT_PREFIX, registry_for_app
from xian.app_logging import build_log_fields
from xian.constants import Constants as c
from xian.exceptions import TransactionException
from xian.log_safety import error_fields
from xian.utils.tx import (
    decode_and_validate_transaction_static_bytes,
    validate_raw_tx_size,
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


def _validate_live(self, tx):
    with (
        self.profiler.scope("check_tx_live_admission"),
        self.client.raw_driver.discarding_changes(),
    ):
        validate_transaction_after_static(
            self.client,
            self.nonce_storage,
            tx,
            fee_policy=self.tx_fee_policy,
        )


def _check_compact(self, raw_tx, cache, storage):
    validate_raw_tx_size(raw_tx, self.max_tx_bytes)
    with self.profiler.scope("check_tx_compact_live"):
        if cache.check_account_live(
            raw_tx,
            *storage,
            max_chi=self.tx_fee_policy.max_tx_chi,
            require_balance=self.tx_fee_policy.require_chi_balance,
        ):
            return
    registry = registry_for_app(self)
    with self.profiler.scope("check_tx_static_admission"):
        tx = decode_and_validate_transaction_static_bytes(
            raw_tx,
            chain_id=self.chain_id,
            account_registry=registry,
        )
    _validate_live(self, tx)
    # A writable driver overlay is never an alternate source of authority.
    # The memo retains exactly the committed bytes used by the verifier.
    cache.remember_account(raw_tx, *registry.committed_authority(tx["payload"]["sender"]))


def _admit_or_decode(self, raw_tx):
    if self.strict_accounts and not raw_tx.startswith(ACCOUNT_PREFIXES):
        raise TransactionException("Hybrid authorization required")
    cache = self.transaction_admission_cache
    storage = self.client.raw_driver.committed_storage()
    usable_cache = cache is not None and cache.chain_id == self.chain_id and storage is not None
    if usable_cache:
        try:
            if raw_tx.startswith(COMPACT_PREFIX):
                _check_compact(self, raw_tx, cache, storage)
                return None
            if not raw_tx.startswith(ACCOUNT_PREFIXES):
                with self.profiler.scope("check_tx_native_admission"):
                    validate_raw_tx_size(raw_tx, self.max_tx_bytes)
                    tx = cache.check_live(
                        raw_tx,
                        *storage,
                        max_chi=self.tx_fee_policy.max_tx_chi,
                        require_balance=self.tx_fee_policy.require_chi_balance,
                    )
                if tx is not None:
                    # Native admission defers overlays and irregular stored values.
                    registry_for_app(self).authorize(tx, raw_tx)
                return tx
        except ValueError as error:
            raise TransactionException(str(error)) from error
    with self.profiler.scope("check_tx_static_admission"):
        return decode_and_validate_transaction_static_bytes(
            raw_tx,
            chain_id=self.chain_id,
            max_raw_tx_bytes=self.max_tx_bytes,
            admission_cache=cache,
            account_registry=registry_for_app(self),
        )


async def check_tx(self, raw_tx) -> ResponseCheckTx:
    try:
        tx = _admit_or_decode(self, raw_tx)
        if tx is None:
            return ResponseCheckTx(code=c.OkCode)
        _validate_live(self, tx)
        return ResponseCheckTx(code=c.OkCode)
    except Exception as e:
        return _rejected_check_tx(self, error=e, raw_tx=raw_tx)
