"""Read and advance the committed nonce for each sender."""

from contracting.constants import DELIMITER, INDEX_SEPARATOR

from xian.constants import Constants
from xian.exceptions import TransactionException


def validate_nonce(nonce: int, latest: int | None) -> None:
    if latest is not None and nonce <= latest:
        raise TransactionException(
            f"Transaction nonce is invalid. Must be greater than {latest}, got {nonce}"
        )


class NonceStorage:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def key(sender: str) -> str:
        return f"{Constants.NONCE_FILENAME}{INDEX_SEPARATOR}{sender}{DELIMITER}"

    def get_nonce(self, sender: str) -> int | None:
        return self.client.raw_driver.value_from_disk(self.key(sender))

    def get_next_nonce(self, sender: str) -> int:
        latest = self.get_nonce(sender)
        return 0 if latest is None else latest + 1

    def set_nonce(self, sender: str, nonce: int) -> None:
        self.client.raw_driver.set(self.key(sender), nonce)

    def set_nonce_by_tx(self, tx: dict) -> None:
        payload = tx["payload"]
        self.set_nonce(payload["sender"], payload["nonce"])


class BlockNonceTracker:
    """Validate against committed state and previously included transactions."""

    def __init__(self, get_committed_nonce):
        self._get_committed_nonce = get_committed_nonce
        self._latest_nonces: dict[str, int] = {}

    def validate_and_advance(self, tx: dict) -> None:
        payload = tx["payload"]
        sender = payload["sender"]
        nonce = payload["nonce"]
        latest = self._latest_nonces.get(sender)
        if latest is None:
            latest = self._get_committed_nonce(sender)
        validate_nonce(nonce, latest)
        self._latest_nonces[sender] = nonce
