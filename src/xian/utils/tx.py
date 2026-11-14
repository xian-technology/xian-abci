import hashlib
import json
from collections.abc import Callable
from copy import deepcopy

from xian_runtime_types.encoding import decode_wire_numbers, encode_wire
from xian_vm_core import decode_and_validate_transaction

from xian.exceptions import TransactionException
from xian.execution_policy import VM_EXECUTION_MODE
from xian.fee_policy import chain_chi_rate


def canonical_json(value: dict) -> str:
    return json.dumps(
        format_dictionary(deepcopy(value)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def tx_hash_from_tx(tx):
    h = hashlib.sha3_256()
    tx_dict = format_dictionary(tx)
    encoded_tx = encode_wire(tx_dict, vm_profile=VM_EXECUTION_MODE).encode("utf-8")
    h.update(encoded_tx)
    return h.hexdigest()


def canonical_transaction_size_bytes(tx: dict) -> int:
    tx_dict = format_dictionary(
        {
            "metadata": deepcopy(tx.get("metadata", {})),
            "payload": deepcopy(tx.get("payload", {})),
        }
    )
    return len(encode_wire(tx_dict, vm_profile=VM_EXECUTION_MODE).encode("utf-8"))


INSUFFICIENT_FEE_BALANCE = (
    "Transaction sender's balance cannot cover the chi_supplied fee reservation"
)
TRANSFER_EXCEEDS_BALANCE = (
    "currency.transfer amount plus the chi_supplied fee reservation exceeds the sender's balance"
)


def check_enough_chi(
    balance: object,
    chi_per_tau: object,
    chi_supplied: object,
    contract: object = None,
    function: object = None,
    amount: object = 0,
):
    """Admit only what execution can pay for.

    Execution first reserves ``chi_supplied / chi_per_tau`` from the sender's
    balance; a currency transfer then spends its amount from what is left.
    """
    if balance * chi_per_tau < chi_supplied:
        raise TransactionException(INSUFFICIENT_FEE_BALANCE)
    if contract == "currency" and function == "transfer":
        if (balance - amount) * chi_per_tau < chi_supplied:
            raise TransactionException(TRANSFER_EXCEEDS_BALANCE)


def validate_transaction_after_static(
    client,
    nonce_storage,
    tx,
    *,
    tx_hash: str,
    fee_policy=None,
):
    if fee_policy is not None:
        fee_policy.validate_tx(tx)
        if not fee_policy.require_chi_balance:
            nonce_storage.check_nonce(tx, tx_hash=tx_hash)
            return

    # Get the senders balance and the current chi rate
    try:
        balance = client.get_var(
            contract="currency",
            variable="balances",
            arguments=[tx["payload"]["sender"]],
        )
    except Exception as e:
        raise TransactionException(f"Failed to retrieve 'currency' balance for sender: {e}")

    try:
        chi_rate = chain_chi_rate(client)
    except Exception as e:
        raise TransactionException(f"Failed to get chi cost: {e}")

    contract = tx["payload"]["contract"]
    func = tx["payload"]["function"]
    chi_supplied = tx["payload"]["chi_supplied"]

    if chi_supplied is None:
        chi_supplied = 0

    if balance is None:
        balance = 0

    # Get how much they are sending
    amount = tx["payload"]["kwargs"].get("amount")
    amount = 0 if amount is None else amount

    if isinstance(amount, dict):
        amount = decode_wire_numbers(amount)

    # Check if they have enough chi for the operation
    check_enough_chi(
        balance,
        chi_rate,
        chi_supplied,
        contract=contract,
        function=func,
        amount=amount,
    )

    # Reserve the local mempool nonce only after all admission checks pass.
    nonce_storage.check_nonce(tx, tx_hash=tx_hash)


def decode_and_validate_transaction_static_bytes(
    raw_tx: bytes,
    *,
    chain_id: str,
    max_raw_tx_bytes: int | None = None,
) -> dict:
    if max_raw_tx_bytes is not None and len(raw_tx) > max_raw_tx_bytes:
        raise TransactionException(
            f"Transaction exceeds maximum configured size of {max_raw_tx_bytes} bytes"
        )
    try:
        return decode_and_validate_transaction(raw_tx, chain_id)
    except ValueError as exc:
        raise TransactionException(str(exc)) from exc


class SequentialNonceTracker:
    """Deterministic per-block/proposal nonce tracker.

    This intentionally does not use node-local mempool pending nonce state.
    It starts from committed nonce state and advances only for transactions
    accepted into the current proposal/block validation pass.
    """

    def __init__(self, committed_nonce_getter: Callable[[str], int | None]):
        self._committed_nonce_getter = committed_nonce_getter
        self._latest_nonces: dict[str, int] = {}

    def expected_nonce(self, sender: str) -> int:
        current_nonce = self._latest_nonces.get(sender)
        if current_nonce is None:
            current_nonce = self._committed_nonce_getter(sender)
        if current_nonce is None:
            return 0
        return current_nonce + 1

    def validate_and_advance(self, tx: dict) -> None:
        sender = tx["payload"]["sender"]
        tx_nonce = tx["payload"]["nonce"]
        expected_nonce = self.expected_nonce(sender)
        if tx_nonce != expected_nonce:
            raise TransactionException(
                f"Transaction nonce is invalid. Expected {expected_nonce}, got {tx_nonce}"
            )
        self._latest_nonces[sender] = tx_nonce


def validate_consensus_transaction_after_static(
    tx: dict,
    *,
    nonce_tracker: SequentialNonceTracker,
) -> None:
    nonce_tracker.validate_and_advance(tx)


def format_dictionary(d: dict) -> dict:
    return _format_value(d)


def _format_value(value):
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            assert isinstance(key, str), "Non-string key types not allowed."
            items.append((key, _format_value(item)))
        return dict(sorted(items))
    if isinstance(value, list):
        return [_format_value(item) for item in value]
    return value
