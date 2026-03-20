from __future__ import annotations

import asyncio
from argparse import ArgumentParser

from xian.constants import Constants
from xian.services.bds.reindex import run_bds_reindex


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Backfill or catch up the local BDS index from CometBFT RPC"
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        help="CometBFT RPC URL to read finalized blocks from",
    )
    parser.add_argument(
        "--start-height",
        type=int,
        help="First block height to backfill (defaults to indexed_height + 1)",
    )
    parser.add_argument(
        "--end-height",
        type=int,
        help="Last block height to backfill (defaults to latest local height)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset the BDS schema and local spool before reindexing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    plan = asyncio.run(
        run_bds_reindex(
            constants=Constants(),
            rpc_url=args.rpc_url,
            start_height=args.start_height,
            end_height=args.end_height,
            reset=args.reset,
        )
    )
    print(
        "BDS reindex complete: "
        f"indexed_height={plan.indexed_height} "
        f"range={plan.start_height}-{plan.end_height} "
        f"latest_height={plan.latest_height}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
