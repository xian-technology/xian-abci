from __future__ import annotations

from argparse import ArgumentParser

from aiohttp import web

from xian.dashboard.app import create_app


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Run the optional Xian dashboard and chain explorer"
    )
    parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657",
        help="CometBFT RPC URL or laddr, for example http://127.0.0.1:26657",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="dashboard listen host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="dashboard listen port",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    web.run_app(
        create_app(args.rpc_url),
        host=args.host,
        port=args.port,
        handle_signals=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
