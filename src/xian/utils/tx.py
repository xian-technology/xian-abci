import hashlib
import json
from copy import deepcopy

import xian_vm_core
from xian_runtime_types.encoding import decode_wire_numbers, encode_wire
from xian_vm_core import decode_and_validate_transaction

from xian.account_registry import ACCOUNT_PREFIXES
from xian.exceptions import TransactionException
from xian.execution_policy import VM_EXECUTION_MODE
from xian.fee_policy import chain_chi_rate
from xian.nonce import validate_nonce
from xian.utils.encoding import canonical_json_text, encode_transaction_bytes

_decode_transaction_for_proposal = getattr(xian_vm_core, "decode_transaction_for_proposal", None)


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


def transaction_bytes(tx: dict) -> bytes:
    """The wire bytes of a transaction document the node built itself (the
    system transactions): the hex of its canonical JSON."""
    return encode_transaction_bytes(
        canonical_json_text({"metadata": tx["metadata"], "payload": tx["payload"]})
    )


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

ADMISSION_CACHE_MAX_ENTRIES = 32_768
ADMISSION_CACHE_MAX_BYTES = 128 * 1024 * 1024


def create_transaction_admission_cache(chain_id: str):
    cache_type = getattr(xian_vm_core, "TransactionAdmissionCache", None)
    if cache_type is None or not hasattr(cache_type, "check_live"):
        raise RuntimeError("The installed Xian VM does not provide native CheckTx admission")
    return cache_type(
        chain_id,
        max_entries=ADMISSION_CACHE_MAX_ENTRIES,
        max_bytes=ADMISSION_CACHE_MAX_BYTES,
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
    fee_policy=None,
):
    payload = tx["payload"]
    validate_nonce(payload["nonce"], nonce_storage.get_nonce(payload["sender"]))
    if fee_policy is not None:
        fee_policy.validate_tx(tx)
        if not fee_policy.require_chi_balance:
            return

    # Get the senders balance and the current chi rate
    try:
        balance = client.get_var(
            contract="currency",
            variable="balances",
            arguments=[payload["sender"]],
        )
    except Exception as e:
        raise TransactionException(f"Failed to retrieve 'currency' balance for sender: {e}")

    try:
        chi_rate = chain_chi_rate(client)
    except Exception as e:
        raise TransactionException(f"Failed to get chi cost: {e}")

    contract = payload["contract"]
    func = payload["function"]
    chi_supplied = payload["chi_supplied"]

    if chi_supplied is None:
        chi_supplied = 0

    if balance is None:
        balance = 0

    # Get how much they are sending
    amount = payload["kwargs"].get("amount")
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


def validate_raw_tx_size(raw_tx: bytes, max_raw_tx_bytes: int | None) -> None:
    if max_raw_tx_bytes is not None and len(raw_tx) > max_raw_tx_bytes:
        raise TransactionException(
            f"Transaction exceeds maximum configured size of {max_raw_tx_bytes} bytes"
        )


def decode_and_validate_transaction_static_bytes(
    raw_tx: bytes,
    *,
    chain_id: str,
    max_raw_tx_bytes: int | None = None,
    admission_cache=None,
    proposal_chi_remaining: int | None = None,
    account_registry=None,
) -> dict | None:
    validate_raw_tx_size(raw_tx, max_raw_tx_bytes)
    try:
        if raw_tx.startswith(ACCOUNT_PREFIXES):
            if account_registry is None:
                raise TransactionException("Hybrid transactions require trusted account state")
            tx = xian_vm_core.decode_account_candidate(raw_tx, chain_id)
            if (
                proposal_chi_remaining is not None
                and tx["payload"]["chi_supplied"] > proposal_chi_remaining
            ):
                return None
        elif proposal_chi_remaining is not None and _decode_transaction_for_proposal is not None:
            tx = _decode_transaction_for_proposal(
                raw_tx, chain_id, proposal_chi_remaining, admission_cache
            )
        elif admission_cache is not None and admission_cache.chain_id == chain_id:
            tx = admission_cache.decode(raw_tx)
        else:
            tx = decode_and_validate_transaction(raw_tx, chain_id)
        if account_registry is not None and tx is not None:
            account_registry.authorize(tx, raw_tx)
        return tx
    except ValueError as exc:
        raise TransactionException(str(exc)) from exc


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
