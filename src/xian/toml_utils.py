from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path
from typing import Any, BinaryIO, TextIO


def loads(payload: str) -> dict[str, Any]:
    return tomllib.loads(payload)


def load(source: str | Path | TextIO | BinaryIO) -> dict[str, Any]:
    if hasattr(source, "read"):
        raw = source.read()
        if isinstance(raw, bytes):
            return tomllib.loads(raw.decode("utf-8"))
        return tomllib.loads(raw)

    path = Path(source)
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def dumps(payload: dict[str, Any]) -> str:
    lines = _dump_table(payload, prefix=())
    return "\n".join(lines) + "\n"


def dump(payload: dict[str, Any], target: TextIO) -> None:
    target.write(dumps(payload))


def _dump_table(
    table: dict[str, Any],
    *,
    prefix: tuple[str, ...],
) -> list[str]:
    lines: list[str] = []
    scalar_items: list[tuple[str, Any]] = []
    nested_items: list[tuple[str, dict[str, Any]]] = []

    for key, value in table.items():
        if isinstance(value, dict):
            nested_items.append((key, value))
        else:
            scalar_items.append((key, value))

    if prefix:
        lines.append(f"[{'.'.join(prefix)}]")

    for key, value in scalar_items:
        lines.append(f"{key} = {_format_value(value)}")

    for key, value in nested_items:
        if lines:
            lines.append("")
        lines.extend(_dump_table(value, prefix=prefix + (key,)))

    return lines


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("non-finite floats are not supported in TOML")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        rendered = ", ".join(_format_value(item) for item in value)
        return f"[{rendered}]"
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")
