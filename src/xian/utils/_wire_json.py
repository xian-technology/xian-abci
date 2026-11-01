"""Serialize node-owned JSON wire documents, not persistent contract values.

Transaction envelopes and loaded genesis/config files may already contain
runtime codec tags. Preserve those JSON objects exactly; applying the strict
runtime encoder to their whole tree would reject legitimate wire data.
"""

import json

from xian_runtime_types.encoding import encode, encode_ints_in_dict


def _runtime_value_to_wire(value):
    # Typed leaves still cross the strict runtime codec boundary. Existing JSON
    # dictionaries never enter this callback, so their tags/quantum are intact.
    return json.loads(encode(value))


def encode_wire_document(document: dict) -> str:
    # Keep the established bigint wrapping, ASCII escaping and compact layout.
    # Storage, state effects and arbitrary contract values must use encode().
    return json.dumps(
        encode_ints_in_dict(document),
        default=_runtime_value_to_wire,
        separators=(",", ":"),
    )
