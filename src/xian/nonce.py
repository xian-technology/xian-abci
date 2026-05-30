import time
from dataclasses import dataclass
from threading import RLock

from contracting import constants as config

from xian.constants import Constants as c
from xian.exceptions import TransactionException

DEFAULT_PENDING_NONCE_RESERVATION_TTL_SECONDS = 60.0
DEFAULT_MAX_PENDING_NONCES_PER_SENDER = 128


@dataclass
class NonceReservation:
    tx_hash: str
    reserved_at: float


class NonceStorage:
    def __init__(
        self,
        client,
        root=None,
        reservation_ttl_seconds: float = DEFAULT_PENDING_NONCE_RESERVATION_TTL_SECONDS,
        max_pending_nonces_per_sender: int = DEFAULT_MAX_PENDING_NONCES_PER_SENDER,
    ):
        root = root if root is not None else c.STORAGE_HOME
        self.client = client
        self.pending_nonces: dict[str, dict[int, NonceReservation]] = {}
        self._pending_lock = RLock()
        self.reservation_ttl_seconds = float(reservation_ttl_seconds)
        self.max_pending_nonces_per_sender = max(
            int(max_pending_nonces_per_sender),
            1,
        )

    def _now(self) -> float:
        return time.monotonic()

    def _prune_pending_for_sender(self, sender: str) -> None:
        with self._pending_lock:
            sender_pending = self.pending_nonces.get(sender)
            if not sender_pending:
                self.pending_nonces.pop(sender, None)
                return

            if self.reservation_ttl_seconds <= 0:
                return

            expires_before = self._now() - self.reservation_ttl_seconds
            for nonce, reservation in list(sender_pending.items()):
                if reservation.reserved_at < expires_before:
                    del sender_pending[nonce]

            if not sender_pending:
                self.pending_nonces.pop(sender, None)

    def _prune_pending(self) -> None:
        with self._pending_lock:
            for sender in list(self.pending_nonces):
                self._prune_pending_for_sender(sender)

    def _get_committed_nonce(self, sender: str) -> int | None:
        return self.get_nonce(sender=sender)

    def check_nonce(self, tx: dict, *, tx_hash: str):
        with self._pending_lock:
            tx_nonce = tx["payload"]["nonce"]
            tx_sender = tx["payload"]["sender"]
            self._prune_pending_for_sender(tx_sender)
            sender_pending = self.pending_nonces.get(tx_sender, {})
            existing = sender_pending.get(tx_nonce)

            # Accept duplicate admission/recheck of the exact same transaction
            # without advancing the sender's local mempool sequence again.
            if existing is not None and existing.tx_hash == tx_hash:
                existing.reserved_at = self._now()
                return

            expected_nonce = self.get_next_nonce(sender=tx_sender)

            if tx_nonce != expected_nonce:
                raise TransactionException(
                    f"Transaction nonce is invalid. Expected {expected_nonce}, got {tx_nonce}"
                )

            if len(sender_pending) >= self.max_pending_nonces_per_sender:
                raise TransactionException("Too many pending transactions reserved for sender")

            sender_pending[tx_nonce] = NonceReservation(
                tx_hash=tx_hash,
                reserved_at=self._now(),
            )
            self.pending_nonces[tx_sender] = sender_pending

    def set_nonce_by_tx(self, tx):
        self.client.raw_driver.set(
            c.NONCE_FILENAME + config.INDEX_SEPARATOR + tx["payload"]["sender"] + config.DELIMITER,
            tx["payload"]["nonce"],
        )

    def set_nonce(self, sender, value):
        self.client.raw_driver.set(
            c.NONCE_FILENAME + config.INDEX_SEPARATOR + sender + config.DELIMITER,
            value,
        )

    # Move this to transaction.py
    def get_nonce(self, sender):
        return self.client.raw_driver.get(
            c.NONCE_FILENAME + config.INDEX_SEPARATOR + sender + config.DELIMITER
        )

    # Move this to transaction.py
    def get_pending_nonce(self, sender):
        with self._pending_lock:
            self._prune_pending_for_sender(sender)
            sender_pending = self.pending_nonces.get(sender, {})
            if not sender_pending:
                return None

            current_nonce = self._get_committed_nonce(sender=sender)
            if current_nonce is None:
                current_nonce = -1
            latest_nonce = current_nonce

            while (latest_nonce + 1) in sender_pending:
                latest_nonce += 1

            if latest_nonce == current_nonce:
                return None
            return latest_nonce

    def safe_set_nonce(self, sender, value):
        current_nonce = self.get_nonce(sender=sender)

        if current_nonce is None:
            current_nonce = -1

        if value > current_nonce:
            self.client.raw_driver.set(
                c.NONCE_FILENAME + config.INDEX_SEPARATOR + sender + config.DELIMITER,
                value,
            )

    def set_pending_nonce(self, sender, value):
        with self._pending_lock:
            current_nonce = self._get_committed_nonce(sender=sender)
            if current_nonce is None:
                current_nonce = -1

            now = self._now()
            sender_pending = {}
            for nonce in range(current_nonce + 1, value + 1):
                sender_pending[nonce] = NonceReservation(
                    tx_hash=f"manual:{sender}:{nonce}",
                    reserved_at=now,
                )
            if sender_pending:
                self.pending_nonces[sender] = sender_pending
            else:
                self.pending_nonces.pop(sender, None)

    # Move this to webserver.py
    def get_latest_nonce(self, sender):
        with self._pending_lock:
            latest_nonce = self.get_pending_nonce(sender=sender)

            if latest_nonce is None:
                latest_nonce = self.get_nonce(sender=sender)

            if latest_nonce is None:
                latest_nonce = 0

            return latest_nonce

    def get_next_nonce(self, sender):
        with self._pending_lock:
            current_nonce = self.get_pending_nonce(sender=sender)

            if current_nonce is None:
                current_nonce = self.get_nonce(sender=sender)

            if current_nonce is None:
                return 0

            return current_nonce + 1

    def reconcile_pending(self):
        with self._pending_lock:
            self._prune_pending()
            for sender, sender_pending in list(self.pending_nonces.items()):
                committed_nonce = self._get_committed_nonce(sender=sender)
                if committed_nonce is None:
                    continue
                for nonce in list(sender_pending):
                    if nonce <= committed_nonce:
                        del sender_pending[nonce]
                if not sender_pending:
                    self.pending_nonces.pop(sender, None)

    def flush(self):
        self.client.raw_driver.flush_file(c.NONCE_FILENAME)
        self.flush_pending()

    def flush_pending(self):
        with self._pending_lock:
            self.pending_nonces.clear()
