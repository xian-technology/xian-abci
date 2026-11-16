"""Capability parsing stays bounded and grants exactly one local Offer."""

import base64
import json
from unittest.mock import patch

import pytest

from xian.evidence_history import HISTORY_ACK_PREFIX, HISTORY_ECHO_PREFIX, EvidenceHistoryPairing

NONCE = "ab" * 32


def envelope(original=b"{}", **changes):
    value = {
        "schema_version": 1,
        "xian_evidence_history_nonce": NONCE,
        "metadata": base64.b64encode(original).decode(),
    }
    value.update(changes)
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    "message",
    [
        "ping",
        HISTORY_ECHO_PREFIX,
        HISTORY_ECHO_PREFIX + "AB" * 32,
        HISTORY_ECHO_PREFIX + "a" * 63,
        HISTORY_ECHO_PREFIX + "g" * 64,
        HISTORY_ACK_PREFIX + NONCE,
    ],
)
def test_only_exact_private_echo_registers_a_capability(message):
    pairing = EvidenceHistoryPairing()
    assert pairing.register_echo(message) is None
    with pytest.raises(ValueError, match="unregistered"):
        pairing.unwrap_offer(envelope(), maximum=64)


def test_each_offer_and_each_process_registration_is_one_use():
    pairing = EvidenceHistoryPairing()
    assert pairing.register_echo(HISTORY_ECHO_PREFIX + NONCE) == HISTORY_ACK_PREFIX + NONCE
    assert pairing.unwrap_offer(envelope(), maximum=2) == b"{}"
    with pytest.raises(ValueError, match="unregistered"):
        pairing.unwrap_offer(envelope(), maximum=2)
    # App restart/reconnect cannot recover a capability from peer metadata.
    with pytest.raises(ValueError, match="unregistered"):
        EvidenceHistoryPairing().unwrap_offer(envelope(), maximum=2)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b"{}",
        envelope(schema_version=True),
        envelope(schema_version=1.0),
        envelope(schema_version=2),
        envelope(extra=True),
        envelope(metadata=None),
        envelope(metadata="###"),
        envelope(metadata="YR=="),
        envelope(metadata="e30=!\n"),
        envelope(xian_evidence_history_nonce="cd" * 32),
        b'{"schema_version":1,"schema_version":1,"xian_evidence_history_nonce":"'
        + NONCE.encode()
        + b'","metadata":"e30="}',
    ],
)
def test_malformed_wrong_and_noncanonical_envelopes_consume_registration(payload):
    pairing = EvidenceHistoryPairing()
    pairing.register_echo(HISTORY_ECHO_PREFIX + NONCE)
    with pytest.raises(ValueError):
        pairing.unwrap_offer(payload, maximum=64)
    with pytest.raises(ValueError, match="unregistered"):
        pairing.unwrap_offer(envelope(), maximum=64)


def test_outer_size_is_checked_before_json_and_decoded_limit_is_exact():
    pairing = EvidenceHistoryPairing()
    pairing.register_echo(HISTORY_ECHO_PREFIX + NONCE)
    with patch("xian.evidence_history.json.loads", side_effect=AssertionError("parsed oversize")):
        with pytest.raises(ValueError, match="limit"):
            pairing.unwrap_offer(b" " * (4 + 256 + 1), maximum=2)
    pairing.register_echo(HISTORY_ECHO_PREFIX + NONCE)
    # Three bytes and two bytes have the same base64 length: decoding needs its own bound.
    with pytest.raises(ValueError, match="encoding"):
        pairing.unwrap_offer(envelope(b"abc"), maximum=2)
    pairing.register_echo(HISTORY_ECHO_PREFIX + NONCE)
    with pytest.raises(ValueError, match="length"):
        pairing.unwrap_offer(envelope(b"abcd"), maximum=2)
    pairing.register_echo(HISTORY_ECHO_PREFIX + NONCE)
    assert pairing.unwrap_offer(envelope(b"ab"), maximum=2) == b"ab"


@pytest.mark.parametrize("maximum, payload", [(2, b"{}"), (4, b"abcd")])
def test_valid_envelope_at_outer_byte_boundary_is_accepted(maximum, payload):
    pairing = EvidenceHistoryPairing()
    pairing.register_echo(HISTORY_ECHO_PREFIX + NONCE)
    original = envelope(payload)
    maximum_outer = 4 * ((maximum + 2) // 3) + 256
    padded = original + b" " * (maximum_outer - len(original))
    assert len(padded) == maximum_outer
    assert pairing.unwrap_offer(padded, maximum=maximum) == payload
