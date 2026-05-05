import re

MIN_JSON_INTEGER = -(2**63)
MAX_JSON_INTEGER = 2**64 - 1
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_CONTRACT_NAME_RE = re.compile(r"^con_[a-zA-Z][a-zA-Z0-9_]*$")
_VK_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-fA-F]{128}$")


def vk_is_formatted(s: str) -> bool:
    return isinstance(s, str) and _VK_RE.fullmatch(s) is not None


def signature_is_formatted(s: str) -> bool:
    return isinstance(s, str) and _SIGNATURE_RE.fullmatch(s) is not None


def identifier_is_formatted(s: str) -> bool:
    return isinstance(s, str) and _IDENTIFIER_RE.fullmatch(s) is not None


def kwargs_are_formatted(kwargs: dict) -> bool:
    return (
        isinstance(kwargs, dict)
        and all(identifier_is_formatted(key) for key in kwargs)
        and json_value_is_formatted(kwargs)
    )


def json_value_is_formatted(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not json_value_is_formatted(item):
                return False
        return True
    if isinstance(value, list):
        return all(json_value_is_formatted(item) for item in value)
    if type(value) is int:
        return MIN_JSON_INTEGER <= value <= MAX_JSON_INTEGER
    return not isinstance(value, float)


def number_is_formatted(i: int) -> bool:
    if type(i) is not int:
        return False
    if i < 0:
        return False
    return i <= MAX_JSON_INTEGER


def cid_id_formated(s: str) -> bool:
    return isinstance(s, str)


def contract_name_is_formatted(s: str) -> bool:
    return isinstance(s, str) and _CONTRACT_NAME_RE.fullmatch(s) is not None


TRANSACTION_PAYLOAD_RULES = {
    "sender": vk_is_formatted,
    "nonce": number_is_formatted,
    "chi_supplied": number_is_formatted,
    "contract": identifier_is_formatted,
    "function": identifier_is_formatted,
    "kwargs": kwargs_are_formatted,
    "chain_id": cid_id_formated,
}

TRANSACTION_METADATA_RULES = {"signature": signature_is_formatted}

TRANSACTION_RULES = {
    "metadata": TRANSACTION_METADATA_RULES,
    "payload": TRANSACTION_PAYLOAD_RULES,
}
