import hashlib
from typing import Callable

from loguru import logger
from xian_accounts import verify_message
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import decode, encode

from xian.exceptions import TransactionException
from xian.formatting import (
    TRANSACTION_PAYLOAD_RULES,
    TRANSACTION_RULES,
    contract_name_is_formatted,
)
from xian.utils.encoding import decode_transaction_bytes

try:
    from xian_fastpath_core import (
        decode_and_validate_transaction_static as _native_decode_and_validate_transaction_static,
    )
except ImportError:  # pragma: no cover - exercised through fallback path
    _native_decode_and_validate_transaction_static = None


def verify(vk: str, msg: str, signature: str):
    return verify_message(vk, msg, signature)


def unpack_transaction(tx):
    timestamp = tx["metadata"].get("timestamp", None)
    if timestamp:
        logger.info("Please remove timestamp from metadata")
    chain_id = tx["payload"].get("chain_id", "")
    if not chain_id:
        logger.debug("Please add chain_id to payload")

    sender = tx["payload"]["sender"]
    signature = tx["metadata"]["signature"]
    tx_for_verification = {
        "chain_id": chain_id,
        "contract": tx["payload"]["contract"],
        "function": tx["payload"]["function"],
        "kwargs": tx["payload"]["kwargs"],
        "nonce": tx["payload"]["nonce"],
        "sender": tx["payload"]["sender"],
        "stamps_supplied": tx["payload"]["stamps_supplied"],
    }
    tx_for_verification = encode(decode(encode(tx_for_verification)))
    return sender, signature, tx_for_verification


def tx_hash_from_tx(tx):
    h = hashlib.sha3_256()
    tx_dict = format_dictionary(tx)
    encoded_tx = encode(tx_dict).encode()
    h.update(encoded_tx)
    return h.hexdigest()


def recurse_rules(d: dict, rule: dict | Callable):
    if callable(rule):
        return rule(d)

    for key, subrule in rule.items():
        arg = d[key]

        if isinstance(arg, dict):
            if not recurse_rules(arg, subrule):
                return False

        elif isinstance(arg, list):
            for a in arg:
                if not recurse_rules(a, subrule):
                    return False

        elif callable(subrule):
            if not subrule(arg):
                return False

    return True


def check_enough_stamps(
    balance: object,
    stamps_per_tau: object,
    stamps_supplied: object,
    contract: object = None,
    function: object = None,
    amount: object = 0,
):

    if balance * stamps_per_tau < stamps_supplied:
        raise TransactionException(
            "Transaction sender has too few stamps for this transaction"
        )

    # Prevent people from sending their entire balances for free by checking if that is what they are doing.
    if contract == "currency" and function == "transfer":
        # If you have less than 2 transactions worth of native token after trying to send your amount, fail.
        if ((balance - amount) * stamps_per_tau) / 6 < 2:
            raise TransactionException(
                "Transaction sender has too few stamps for this transaction"
            )


def check_format(d: dict, rule: dict):
    expected_keys = set(rule.keys())

    if not dict_has_keys(d, expected_keys):
        raise TransactionException("Transaction has unexpected or missing keys")
    if not recurse_rules(d, rule):
        raise TransactionException(
            "Transaction has wrongly formatted dictionary"
        )


def check_tx_keys(tx):
    metadata = tx.get("metadata")

    if not metadata:
        raise TransactionException("Metadata is missing")
    if len(metadata.keys()) != 1:
        raise TransactionException("Wrong number of metadata entries")

    payload = tx.get("payload")

    if not payload:
        raise TransactionException("Payload is missing")
    if not payload["sender"]:
        raise TransactionException("Payload key 'sender' is missing")
    if not payload["contract"]:
        raise TransactionException("Payload key 'contract' is missing")
    if not payload["function"]:
        raise TransactionException("Payload key 'function' is missing")
    if not payload["stamps_supplied"]:
        raise TransactionException("Payload key 'stamps_supplied' is missing")

    keys = list(payload.keys())
    keys_are_valid = list(
        map(lambda key: key in keys, list(TRANSACTION_PAYLOAD_RULES.keys()))
    )

    if not all(keys_are_valid) and len(keys) == len(
        list(TRANSACTION_PAYLOAD_RULES.keys())
    ):
        raise TransactionException("Payload keys are not valid")


def check_tx_formatting(tx: dict):
    check_tx_keys(tx)
    check_format(tx, TRANSACTION_RULES)


def check_contract_name(contract, function, name):
    if (
        contract == "submission"
        and function == "submit_contract"
        and (len(name) > 255 or not contract_name_is_formatted(name))
    ):
        raise TransactionException("Transaction contract name is invalid")


def validate_transaction(
    client,
    nonce_storage,
    tx,
    *,
    tx_hash: str,
    chain_id: str,
):
    validate_transaction_static(tx, chain_id=chain_id)
    validate_transaction_after_static(
        client,
        nonce_storage,
        tx,
        tx_hash=tx_hash,
    )

def validate_transaction_after_static(
    client,
    nonce_storage,
    tx,
    *,
    tx_hash: str,
):
    # Reserve the local mempool nonce only after static validation passes.
    nonce_storage.check_nonce(tx, tx_hash=tx_hash)

    # Get the senders balance and the current stamp rate
    try:
        balance = client.get_var(
            contract="currency",
            variable="balances",
            arguments=[tx["payload"]["sender"]],
            mark=False,
        )
    except Exception as e:
        raise TransactionException(
            f"Failed to retrieve 'currency' balance for sender: {e}"
        )

    try:
        stamp_rate = client.get_var(
            contract="stamp_cost", variable="S", arguments=["value"], mark=False
        )
        if stamp_rate is None:
            stamp_rate = 20
    except Exception as e:
        raise TransactionException(f"Failed to get stamp cost: {e}")

    contract = tx["payload"]["contract"]
    func = tx["payload"]["function"]
    stamps_supplied = tx["payload"]["stamps_supplied"]

    if stamps_supplied is None:
        stamps_supplied = 0

    if balance is None:
        balance = 0

    # Get how much they are sending
    amount = tx["payload"]["kwargs"].get("amount")
    amount = 0 if amount is None else amount

    if isinstance(amount, dict) and "__fixed__" in amount:
        amount = ContractingDecimal(amount["__fixed__"])

    # Check if they have enough stamps for the operation
    check_enough_stamps(
        balance,
        stamp_rate,
        stamps_supplied,
        contract=contract,
        function=func,
        amount=amount,
    )


def decode_and_validate_transaction_static_bytes(
    raw_tx: bytes,
    *,
    chain_id: str,
) -> dict:
    if _native_decode_and_validate_transaction_static is not None:
        try:
            return _native_decode_and_validate_transaction_static(
                raw_tx,
                chain_id,
            )
        except Exception as exc:
            raise TransactionException(str(exc)) from exc

    tx, _ = decode_transaction_bytes(raw_tx)
    validate_transaction_static(tx, chain_id=chain_id)
    return tx


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


def validate_transaction_static(tx: dict, *, chain_id: str) -> None:
    check_tx_formatting(tx)

    sender, signature, payload = unpack_transaction(tx)
    if not verify(sender, payload, signature):
        raise TransactionException("Bad signature")

    tx_chain_id = tx["payload"].get("chain_id", "")
    if tx_chain_id != chain_id:
        raise TransactionException("Wrong chain_id")

    name = tx["payload"]["kwargs"].get("name")
    check_contract_name(
        tx["payload"]["contract"],
        tx["payload"]["function"],
        name,
    )


def validate_consensus_transaction(
    tx: dict,
    *,
    chain_id: str,
    nonce_tracker: SequentialNonceTracker,
) -> None:
    validate_transaction_static(tx, chain_id=chain_id)
    validate_consensus_transaction_after_static(
        tx,
        nonce_tracker=nonce_tracker,
    )


def validate_consensus_transaction_after_static(
    tx: dict,
    *,
    nonce_tracker: SequentialNonceTracker,
) -> None:
    nonce_tracker.validate_and_advance(tx)


def dict_has_keys(d: dict, keys: set):
    key_set = set(d.keys())
    return len(keys ^ key_set) == 0


def format_dictionary(d: dict) -> dict:
    for k, v in d.items():
        assert isinstance(k, str), "Non-string key types not allowed."
        if isinstance(v, list):
            for i in range(len(v)):
                if isinstance(v[i], dict):
                    v[i] = format_dictionary(v[i])
        elif isinstance(v, dict):
            d[k] = format_dictionary(v)
    return {k: v for k, v in sorted(d.items())}
