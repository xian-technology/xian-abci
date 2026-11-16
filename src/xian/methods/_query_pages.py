"""Bounded archive reads that never materialize the complete contract state."""

from urllib.parse import unquote

from xian_vm_core import MAX_STORAGE_KEY_BYTES

MAX_ARCHIVE_SCAN = 1000
MAX_LEGACY_CONTRACTS = 100


def stored_key_exists(driver, key: str) -> bool:
    if len(key.encode("utf-8")) > MAX_STORAGE_KEY_BYTES:
        return False
    keys, _ = driver.scan_keys_from_disk(key, limit=1)
    return bool(keys) and keys[0] == key


def _contract_presence(driver, name: str) -> tuple[bool, bool]:
    source = stored_key_exists(driver, f"{name}.__source__")
    return source, source or stored_key_exists(driver, f"{name}.__xian_ir_v1__")


def parse_contract_cursor(raw: str | None) -> str | None:
    if not raw:
        return None
    after = unquote(raw, errors="strict")
    if len(after.encode("utf-8")) > MAX_STORAGE_KEY_BYTES or any(
        char in after for char in "./\x00"
    ):
        raise ValueError("Contract cursor must be a bounded namespace without dot, slash or NUL.")
    return after


def _namespace_boundary(name: str) -> str:
    # A maximal-length global key cannot have child keys; skip it exactly.
    return name if len(name.encode("utf-8")) == MAX_STORAGE_KEY_BYTES else f"{name}/"


def contract_names_page(driver, *, limit: int, after: str | None = None):
    """Seek past each visited ``name.*`` range without reading its values.

    The slash immediately after a namespace sorts above its dot separator
    and below identifier suffix characters. This skips every storage row in
    that namespace, including arbitrarily large token/contract maps.
    """
    seek = _namespace_boundary(after) if after is not None else None
    records = []
    last = after
    for _ in range(MAX_ARCHIVE_SCAN):
        keys, _ = driver.scan_keys_from_disk("", limit=1, after_key=seek)
        if not keys:
            return records, None
        last = keys[0].partition(".")[0]
        seek = _namespace_boundary(last)
        source, exists = _contract_presence(driver, last)
        if exists:
            records.append((last, source))
        if len(records) >= limit:
            following, _ = driver.scan_keys_from_disk("", limit=1, after_key=seek)
            return records, last if following else None
    return records, last


def open_votes_page(driver, *, limit: int, cursor: int | None = None) -> dict:
    """Scan at most one bounded range of newest-first proposal IDs."""
    total = driver.get("validators.total_votes") or 0
    current = total if cursor is None else cursor
    if current > total:
        raise ValueError("Proposal cursor is beyond the current archive.")
    items = []
    scanned = 0
    while current > 0 and scanned < MAX_ARCHIVE_SCAN and len(items) < limit:
        record = driver.get(f"validators.votes:{current}")
        if _is_pending_vote(record):
            items.append({**record, "proposal_id": current})
        current -= 1
        scanned += 1
    return {"items": items, "next_cursor": current if current > 0 else None, "limit": limit}


def _is_pending_vote(record) -> bool:
    return (
        isinstance(record, dict)
        and record.get("finalized") is not True
        and record.get("status") == "pending"
    )
