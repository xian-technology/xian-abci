from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from xian.genesis_builder import write_genesis_block
from xian.recovery_genesis import build_recovery_genesis
from xian.state_export import load_exported_state


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description=(
            "Build the genesis that restarts a chain halted by offline voting "
            "power from its exported last state, keeping only the given validators"
        )
    )
    parser.add_argument("--exported-state", required=True, help="xian-export-state output")
    parser.add_argument("--keep", action="append", required=True, help="validator key to keep")
    parser.add_argument("--chain-id", required=True)
    parser.add_argument("--genesis-time", required=True, help="agreed by all operators")
    parser.add_argument("--output", required=True, help="CometBFT genesis.json to write")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    genesis = build_recovery_genesis(
        load_exported_state(Path(args.exported_state)),
        args.keep,
        chain_id=args.chain_id,
        genesis_time=args.genesis_time,
    )
    write_genesis_block(Path(args.output), genesis)
    print(
        f"Recovery genesis for {args.chain_id} at height {genesis['initial_height']} "
        f"(app hash {genesis['app_hash']}, {len(genesis['validators'])} validators) "
        f'written to "{args.output}"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
