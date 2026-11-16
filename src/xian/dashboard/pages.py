"""Bounded query pages shared by the dashboard's read-only routes."""


def numeric_cursor(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Cursor must be a positive integer.")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("Cursor must be a positive integer.")
    return parsed


def namespace_cursor(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 511:
        raise ValueError("Contract cursor must be a namespace of at most 511 UTF-8 bytes.")
    return value


def _page_items(payload, items_key, limit):
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid cursor page from node.")
    items = payload.get(items_key)
    if (
        not isinstance(items, list)
        or len(items) > limit
        or any(not isinstance(item, dict) for item in items)
    ):
        raise RuntimeError("Invalid or oversized cursor page from node.")
    return items


def bounded_page(payload, *, items_key, next_key, limit, cursor, parse_cursor, descending=False):
    items = _page_items(payload, items_key, limit)
    if next_key not in payload:
        raise RuntimeError("Cursor page is missing continuation information.")
    following = parse_cursor(payload[next_key])
    if following is not None and cursor is not None:
        advances = following < cursor if descending else following > cursor
        if not advances:
            raise RuntimeError("Node cursor did not advance.")
    return items, following
