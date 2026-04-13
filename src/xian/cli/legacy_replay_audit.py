from __future__ import annotations

import asyncio
from argparse import ArgumentParser
from pathlib import Path

from xian.legacy_network_replay import (
    build_summary_line,
    run_legacy_network_replay_audit,
)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description=(
            "Replay a legacy Xian network transaction range against the "
            "current Python and xian_vm_v1 runtimes."
        )
    )
    parser.add_argument(
        "--rpc-url",
        required=True,
        help="Legacy network CometBFT RPC URL, for example https://node.xian.org",
    )
    parser.add_argument(
        "--graphql-url",
        required=True,
        help="Legacy network GraphQL URL, for example https://node.xian.org/graphql",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for the replay report, transaction log, and replay state",
    )
    parser.add_argument(
        "--start-height",
        type=int,
        help="First block height to replay. Defaults to the first non-GENESIS tx height.",
    )
    parser.add_argument(
        "--end-height",
        type=int,
        help="Last block height to replay. Defaults to the current legacy chain tip.",
    )
    parser.add_argument(
        "--max-transactions",
        type=int,
        help="Optional hard cap on replayed transactions for faster audit slices.",
    )
    parser.add_argument(
        "--logic-only",
        action="store_true",
        help=(
            "Skip strict historical fee/reward parity and run only logic parity "
            "comparisons based on status/result/events."
        ),
    )
    parser.add_argument(
        "--native-only",
        action="store_true",
        help=(
            "Skip the current Python replay path and focus only on whether the "
            "new xian_vm_v1 can process the historical transactions."
        ),
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=500,
        help="Emit a progress line every N processed transactions. Use 0 to disable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = asyncio.run(
        run_legacy_network_replay_audit(
            rpc_url=args.rpc_url,
            graphql_url=args.graphql_url,
            output_dir=args.output_dir,
            start_height=args.start_height,
            end_height=args.end_height,
            max_transactions=args.max_transactions,
            logic_only=args.logic_only,
            native_only=args.native_only,
            progress_every=args.progress_every,
        )
    )
    print(build_summary_line(report))
    print(f"report={args.output_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
