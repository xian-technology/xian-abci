from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

Shape = tuple[str, str]


def _shape_from_record(record: Any) -> tuple[Shape, int] | None:
    if not isinstance(record, dict):
        return None
    contract = record.get("contract")
    function = record.get("function")
    count = record.get("count", 0)
    if not isinstance(contract, str) or not isinstance(function, str):
        return None
    try:
        normalized_count = int(count)
    except TypeError, ValueError:
        return None
    if normalized_count <= 0:
        return None
    return (contract, function), normalized_count


def _iter_blocks(snapshot: Any):
    if isinstance(snapshot, dict):
        recent_blocks = snapshot.get("recent_blocks")
        if isinstance(recent_blocks, list):
            yield from (
                block for block in recent_blocks if isinstance(block, dict)
            )
            return
        if isinstance(snapshot.get("metadata"), dict):
            yield snapshot
            return
    if isinstance(snapshot, list):
        yield from (block for block in snapshot if isinstance(block, dict))


def _add_shapes(
    counter: Counter[Shape],
    records: Any,
) -> None:
    if not isinstance(records, list | tuple):
        return
    for record in records:
        parsed = _shape_from_record(record)
        if parsed is None:
            continue
        shape, count = parsed
        counter[shape] += count


def _format_shapes(
    counter: Counter[Shape],
    *,
    limit: int,
) -> list[dict[str, object]]:
    return [
        {
            "contract": contract,
            "function": function,
            "count": count,
        }
        for (contract, function), count in sorted(
            counter.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )[:limit]
    ]


def summarize_snapshots(
    snapshots: list[Any],
    *,
    limit: int = 20,
) -> dict[str, object]:
    known: Counter[Shape] = Counter()
    unknown: Counter[Shape] = Counter()
    blocks_seen = 0
    parallel_blocks = 0
    estimated_known_transactions = 0
    estimated_unknown_transactions = 0

    for snapshot in snapshots:
        for block in _iter_blocks(snapshot):
            blocks_seen += 1
            metadata = block.get("metadata") or {}
            if not isinstance(metadata, dict):
                continue
            if metadata.get("parallel_enabled"):
                parallel_blocks += 1
            estimated_known_transactions += int(
                metadata.get("parallel_estimated_known_transactions") or 0
            )
            estimated_unknown_transactions += int(
                metadata.get("parallel_estimated_unknown_transactions") or 0
            )
            _add_shapes(
                known,
                metadata.get("parallel_estimated_known_shapes"),
            )
            _add_shapes(
                unknown,
                metadata.get("parallel_estimated_unknown_shapes"),
            )

    return {
        "blocks_seen": blocks_seen,
        "parallel_blocks": parallel_blocks,
        "estimated_known_transactions": estimated_known_transactions,
        "estimated_unknown_transactions": estimated_unknown_transactions,
        "known_shapes": _format_shapes(known, limit=limit),
        "unknown_shapes": _format_shapes(unknown, limit=limit),
    }


def load_snapshots(paths: list[Path]) -> list[Any]:
    snapshots = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            snapshots.append(json.load(handle))
    return snapshots


def _print_text(summary: dict[str, object]) -> None:
    print(f"blocks_seen: {summary['blocks_seen']}")
    print(f"parallel_blocks: {summary['parallel_blocks']}")
    print(
        f"estimated_known_transactions: {summary['estimated_known_transactions']}"
    )
    print(
        "estimated_unknown_transactions: "
        f"{summary['estimated_unknown_transactions']}"
    )

    print("\nunknown_shapes:")
    unknown_shapes = summary.get("unknown_shapes") or []
    if unknown_shapes:
        for shape in unknown_shapes:
            print(
                f"  {shape['count']:>6}  "
                f"{shape['contract']}.{shape['function']}"
            )
    else:
        print("  none")

    print("\nknown_shapes:")
    known_shapes = summary.get("known_shapes") or []
    if known_shapes:
        for shape in known_shapes:
            print(
                f"  {shape['count']:>6}  "
                f"{shape['contract']}.{shape['function']}"
            )
    else:
        print("  none")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize parallel access-estimator coverage from /perf_status "
            "or xian-perf.json snapshots."
        )
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="JSON snapshot files to inspect",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum known/unknown shapes to show",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = summarize_snapshots(
        load_snapshots(args.paths),
        limit=max(int(args.limit), 1),
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
