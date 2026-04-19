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
    parser.add_argument(
        "--max-ws-clients",
        type=int,
        default=100,
        help="maximum concurrent browser websocket clients",
    )
    parser.add_argument(
        "--max-state-subs-per-client",
        type=int,
        default=64,
        help="maximum state subscriptions per websocket client",
    )
    parser.add_argument(
        "--max-event-subs-per-client",
        type=int,
        default=32,
        help="maximum event subscriptions per websocket client",
    )
    parser.add_argument(
        "--max-ws-message-bytes",
        type=int,
        default=64 * 1024,
        help="maximum inbound websocket message size in bytes",
    )
    parser.add_argument(
        "--max-ws-outbound-queue",
        type=int,
        default=128,
        help="maximum queued outbound websocket messages per client",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    web.run_app(
        create_app(
            args.rpc_url,
            max_ws_clients=args.max_ws_clients,
            max_state_subscriptions_per_client=args.max_state_subs_per_client,
            max_event_subscriptions_per_client=args.max_event_subs_per_client,
            max_ws_message_bytes=args.max_ws_message_bytes,
            max_ws_outbound_queue=args.max_ws_outbound_queue,
        ),
        host=args.host,
        port=args.port,
        handle_signals=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
