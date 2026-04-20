from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

from xian.constants import Constants
from xian.toml_utils import load as load_toml


def normalize_rpc_url(address: str) -> str:
    normalized = address.strip()
    if normalized.startswith(("http://", "https://")):
        return normalized.rstrip("/")
    normalized = normalized.replace("tcp://", "").replace("unix://", "")
    return f"http://{normalized.rstrip('/')}"


def resolve_local_rpc_url(
    address: str,
    *,
    default_host: str = "127.0.0.1",
    default_port: int = 26657,
) -> str:
    normalized = normalize_rpc_url(address)
    parts = urlsplit(normalized)
    host = parts.hostname or default_host
    if host in {"0.0.0.0", "::"}:
        netloc = f"{default_host}:{parts.port or default_port}"
        return urlunsplit((parts.scheme or "http", netloc, parts.path, "", ""))
    return normalized


def load_tendermint_config(config: Constants):
    if not (config.COMETBFT_HOME.exists() and config.COMETBFT_HOME.is_dir()):
        raise FileNotFoundError("You must initialize CometBFT first")
    if not (
        config.COMETBFT_CONFIG.exists() and config.COMETBFT_CONFIG.is_file()
    ):
        raise FileNotFoundError(f"File not found: {config.COMETBFT_CONFIG}")

    return load_toml(config.COMETBFT_CONFIG)


def load_genesis_data(config: Constants):
    if not (
        config.COMETBFT_GENESIS.exists() and config.COMETBFT_GENESIS.is_file()
    ):
        raise FileNotFoundError(f"File not found: {config.COMETBFT_GENESIS}")

    with open(config.COMETBFT_GENESIS, "r") as file:
        return json.load(file)
