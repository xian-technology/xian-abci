"""Ephemeral local capability pairing for verified Comet snapshot bootstrap.

Only the private ABCI Echo connection can register a capability. It is never
chain state, peer metadata, a public query response, or a persisted setting.
"""

import base64
import json
import re

HISTORY_ECHO_PREFIX = "xian-evidence-history/1:"
HISTORY_ACK_PREFIX = HISTORY_ECHO_PREFIX + "registered:"
_ENVELOPE_KEYS = {"schema_version", "xian_evidence_history_nonce", "metadata"}


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate evidence history envelope key")
        result[key] = value
    return result


def _parse_envelope(metadata: bytes, maximum: int) -> dict:
    # The original metadata is base64, plus a small fixed-schema local envelope.
    if len(metadata) > 4 * ((maximum + 2) // 3) + 256:
        raise ValueError("evidence history envelope exceeds metadata limit")
    value = json.loads(metadata, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != _ENVELOPE_KEYS:
        raise ValueError("invalid evidence history envelope")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported evidence history envelope schema")
    return value


def _decode_metadata(encoded: object, maximum: int) -> bytes:
    if type(encoded) is not str or len(encoded) > 4 * ((maximum + 2) // 3):
        raise ValueError("invalid evidence history metadata length")
    # Re-encoding rejects ignored characters AND noncanonical padding bits.
    original = base64.b64decode(encoded)
    if len(original) > maximum or base64.b64encode(original).decode() != encoded:
        raise ValueError("invalid evidence history metadata encoding")
    return original


class EvidenceHistoryPairing:
    def __init__(self):
        self._nonce = None

    def register_echo(self, message: str) -> str | None:
        if not message.startswith(HISTORY_ECHO_PREFIX):
            return None
        nonce = message[len(HISTORY_ECHO_PREFIX) :]
        if re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
            return None
        self._nonce = nonce
        return HISTORY_ACK_PREFIX + nonce

    def unwrap_offer(self, metadata: bytes, *, maximum: int) -> bytes:
        # Each attempted Offer consumes the one local registration, even when
        # its envelope or original peer metadata is invalid. Go must Echo anew.
        expected, self._nonce = self._nonce, None
        if expected is None:
            raise ValueError("unregistered evidence history client")
        value = _parse_envelope(metadata, maximum)
        if value["xian_evidence_history_nonce"] != expected:
            raise ValueError("unregistered evidence history offer")
        return _decode_metadata(value["metadata"], maximum)
