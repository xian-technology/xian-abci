from __future__ import annotations

from argparse import ArgumentParser

from aiohttp import web

from xian.dashboard.app import (
    DEFAULT_EXPENSIVE_REST_RATE_LIMIT_BURST,
    DEFAULT_EXPENSIVE_REST_RATE_LIMIT_PER_SECOND,
    DEFAULT_MAX_EVENT_SUBSCRIPTIONS_PER_CLIENT,
    DEFAULT_MAX_REST_CONCURRENCY,
    DEFAULT_MAX_STATE_SUBSCRIPTIONS_PER_CLIENT,
    DEFAULT_MAX_WS_CLIENTS,
    DEFAULT_MAX_WS_CLIENTS_PER_CLIENT,
    DEFAULT_MAX_WS_MESSAGE_BYTES,
    DEFAULT_MAX_WS_OUTBOUND_QUEUE,
    DEFAULT_RATE_LIMIT_MAX_KEYS,
    DEFAULT_REST_RATE_LIMIT_BURST,
    DEFAULT_REST_RATE_LIMIT_PER_SECOND,
    create_app,
)


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
        default=DEFAULT_MAX_WS_CLIENTS,
        help="maximum concurrent browser websocket clients",
    )
    parser.add_argument(
        "--max-ws-clients-per-client",
        type=int,
        default=DEFAULT_MAX_WS_CLIENTS_PER_CLIENT,
        help="maximum concurrent browser websocket clients per remote client",
    )
    parser.add_argument(
        "--max-state-subs-per-client",
        type=int,
        default=DEFAULT_MAX_STATE_SUBSCRIPTIONS_PER_CLIENT,
        help="maximum state subscriptions per websocket client",
    )
    parser.add_argument(
        "--max-event-subs-per-client",
        type=int,
        default=DEFAULT_MAX_EVENT_SUBSCRIPTIONS_PER_CLIENT,
        help="maximum event subscriptions per websocket client",
    )
    parser.add_argument(
        "--max-ws-message-bytes",
        type=int,
        default=DEFAULT_MAX_WS_MESSAGE_BYTES,
        help="maximum inbound websocket message size in bytes",
    )
    parser.add_argument(
        "--max-ws-outbound-queue",
        type=int,
        default=DEFAULT_MAX_WS_OUTBOUND_QUEUE,
        help="maximum queued outbound websocket messages per client",
    )
    parser.add_argument(
        "--rest-rate-limit-per-second",
        type=float,
        default=DEFAULT_REST_RATE_LIMIT_PER_SECOND,
        help="per-client REST/API token refill rate",
    )
    parser.add_argument(
        "--rest-rate-limit-burst",
        type=int,
        default=DEFAULT_REST_RATE_LIMIT_BURST,
        help="per-client REST/API burst capacity",
    )
    parser.add_argument(
        "--expensive-rest-rate-limit-per-second",
        type=float,
        default=DEFAULT_EXPENSIVE_REST_RATE_LIMIT_PER_SECOND,
        help="per-client refill rate for expensive dashboard API routes",
    )
    parser.add_argument(
        "--expensive-rest-rate-limit-burst",
        type=int,
        default=DEFAULT_EXPENSIVE_REST_RATE_LIMIT_BURST,
        help="per-client burst capacity for expensive dashboard API routes",
    )
    parser.add_argument(
        "--max-rest-concurrency",
        type=int,
        default=DEFAULT_MAX_REST_CONCURRENCY,
        help="maximum concurrent dashboard REST/API requests",
    )
    parser.add_argument(
        "--rate-limit-max-keys",
        type=int,
        default=DEFAULT_RATE_LIMIT_MAX_KEYS,
        help="maximum in-memory dashboard rate limit keys",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    web.run_app(
        create_app(
            args.rpc_url,
            max_ws_clients=args.max_ws_clients,
            max_ws_clients_per_client=args.max_ws_clients_per_client,
            max_state_subscriptions_per_client=args.max_state_subs_per_client,
            max_event_subscriptions_per_client=args.max_event_subs_per_client,
            max_ws_message_bytes=args.max_ws_message_bytes,
            max_ws_outbound_queue=args.max_ws_outbound_queue,
            rest_rate_limit_per_second=args.rest_rate_limit_per_second,
            rest_rate_limit_burst=args.rest_rate_limit_burst,
            expensive_rest_rate_limit_per_second=(
                args.expensive_rest_rate_limit_per_second
            ),
            expensive_rest_rate_limit_burst=(
                args.expensive_rest_rate_limit_burst
            ),
            max_rest_concurrency=args.max_rest_concurrency,
            rate_limit_max_keys=args.rate_limit_max_keys,
        ),
        host=args.host,
        port=args.port,
        handle_signals=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
