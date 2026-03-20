from __future__ import annotations

import asyncio
from argparse import ArgumentParser

from xian.constants import Constants
from xian.services.bds.bds import BDS
from xian.services.bds.runtime import resolve_bds_config


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Safely inspect, drain, or compact the local BDS spool"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compact = subparsers.add_parser("compact")
    compact.add_argument(
        "--offline",
        action="store_true",
        help="Acknowledge that the node should be stopped before compacting",
    )

    drain = subparsers.add_parser("drain")
    drain.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Maximum time to wait for the worker to persist pending spool entries",
    )
    drain.add_argument(
        "--offline",
        action="store_true",
        help="Acknowledge that the node should be stopped before draining the spool",
    )
    return parser


def _require_offline(command: str, acknowledged: bool) -> None:
    if acknowledged:
        return
    raise SystemExit(
        f"`xian-bds-spool {command}` should be run with the node stopped. "
        f"Re-run with --offline once that is true."
    )


async def _run_compact() -> dict:
    bds = BDS(config=resolve_bds_config(Constants()))
    try:
        await bds.open_storage()
        await bds.ensure_schema()
        compacted = await bds.compact_spool()
        status = await bds.get_status()
        return {
            "compacted": compacted,
            "status": status,
        }
    finally:
        await bds.close()


async def _run_drain(*, timeout_seconds: float) -> dict:
    bds = BDS(config=resolve_bds_config(Constants()))
    try:
        await bds.open_storage()
        await bds.ensure_schema()
        return await bds.drain_spool(timeout_seconds=timeout_seconds)
    finally:
        await bds.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "compact":
        _require_offline("compact", args.offline)
        result = asyncio.run(_run_compact())
        compacted = result["compacted"]
        print(
            "BDS spool compacted: "
            f"indexed_height={compacted['indexed_height']} "
            f"removed_files={compacted['removed_files']} "
            f"kept_files={compacted['kept_files']}"
        )
    else:
        _require_offline("drain", args.offline)
        result = asyncio.run(_run_drain(timeout_seconds=args.timeout_seconds))
        print(
            "BDS spool drain complete: "
            f"timed_out={result['timed_out']} "
            f"pending={result['status']['spool_pending_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
