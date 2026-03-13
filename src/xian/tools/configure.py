from __future__ import annotations

import json
from argparse import ArgumentParser, BooleanOptionalAction

from xian.node_admin import configure_existing_home


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Configure CometBFT")
    parser.add_argument(
        "--seed-node",
        type=str,
        help=(
            "seed node host or IP without port; queries node ID from the "
            "remote status endpoint"
        ),
        required=False,
    )
    parser.add_argument(
        "--seed-node-address",
        type=str,
        help=(
            "seed node address in <node_id>@<host> form; used without a "
            "remote lookup"
        ),
        required=False,
    )
    parser.add_argument(
        "--moniker",
        type=str,
        help="name of your node",
        required=True,
    )
    parser.add_argument(
        "--allow-cors",
        action=BooleanOptionalAction,
        help="allow CORS on the RPC endpoint",
        required=False,
        default=True,
    )
    parser.add_argument(
        "--snapshot-url",
        type=str,
        help="URL of snapshot in tar.gz or tar format",
        required=False,
    )
    parser.add_argument(
        "--copy-genesis",
        action=BooleanOptionalAction,
        help="copy a genesis file into the configured CometBFT home",
        required=True,
    )
    parser.add_argument(
        "--genesis-file-name",
        type=str,
        help="legacy genesis filename to copy when --copy-genesis is enabled",
        required=False,
        default="genesis-testnet.json",
    )
    parser.add_argument(
        "--validator-privkey",
        type=str,
        help="validator private key as a 64-character hex string",
        required=True,
    )
    parser.add_argument(
        "--prometheus",
        action=BooleanOptionalAction,
        help="enable Prometheus metrics",
        required=False,
        default=True,
    )
    parser.add_argument(
        "--service-node",
        action=BooleanOptionalAction,
        help="enable service-node mode",
        required=False,
        default=False,
    )
    parser.add_argument(
        "--enable-pruning",
        action=BooleanOptionalAction,
        help='prune blocks according to "blocks-to-keep"',
        required=False,
        default=False,
    )
    parser.add_argument(
        "--blocks-to-keep",
        type=int,
        help='number of blocks to keep when "enable-pruning" is enabled',
        required=False,
        default=100000,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = configure_existing_home(
        moniker=args.moniker,
        validator_private_key_hex=args.validator_privkey,
        allow_cors=args.allow_cors,
        seed_node=args.seed_node,
        seed_node_address=args.seed_node_address,
        snapshot_url=args.snapshot_url,
        copy_genesis=args.copy_genesis,
        genesis_file_name=args.genesis_file_name,
        prometheus=args.prometheus,
        service_node=args.service_node,
        enable_pruning=args.enable_pruning,
        blocks_to_keep=args.blocks_to_keep,
    )
    print("Make sure that port 26657 is open for the REST API")
    print("Make sure that port 26656 is open for P2P Node communication")
    print("Configuration updated")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
