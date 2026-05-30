from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from xian.state_export import export_state


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Export file-based chain state")
    parser.add_argument("-k", "--key", type=str, required=False)
    parser.add_argument("--output-path", type=str, required=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = Path(args.output_path) if args.output_path is not None else Path.cwd()
    output_path = export_state(
        output_dir=output_dir,
        founder_private_key=args.key,
    )
    print(f'Saving genesis block to "{output_path}"...')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
