"""Frozen outputs of the per-transaction path, for tests that compared it
with the Rust executor (single-engine plan, increment 9).

Until increment 9 these tests ran every case on both paths and compared
them. The per-transaction path's outputs were frozen into
``tests/fixtures/golden/<name>.json`` (where the Rust executor reproduced
them byte for byte) before that path was deleted. They are not regenerated:
a difference is a behaviour change of the engine.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from xian_runtime_types.encoding import encode

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
# An entry whose wire JSON is longer is stored as its SHA-256 and length.
LARGE_ENTRY = 4096


def _plain(value: Any) -> Any:
    """``value`` with bytes as text and tuples as lists (receipt entries)."""
    if isinstance(value, bytes):
        return {"__bytes_utf8__": value.decode("utf-8")}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _stored(text: str) -> Any:
    if len(text) <= LARGE_ENTRY:
        return json.loads(text)
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}


def wire(entries: list) -> list:
    """Each entry as the fixture stores it: its wire JSON value, or the
    digest of a large one."""
    return [_stored(encode(_plain(entry), vm_profile="xian_vm_v2")) for entry in entries]


def load_golden(name: str) -> list:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def assert_matches_golden(name: str, entries: list) -> None:
    """``entries`` (receipts, pending state, log records) equal the frozen
    outputs of ``name``, entry by entry."""
    expected = load_golden(name)
    actual = wire(entries)
    assert len(actual) == len(expected), name
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert left == right, f"{name}: entry {index}"
