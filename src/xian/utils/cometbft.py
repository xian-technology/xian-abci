from __future__ import annotations

import json
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

from xian.constants import Constants
from xian.toml_utils import load as load_toml


def _strip_host_brackets(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _is_ipv6_literal(host: str) -> bool:
    try:
        return ip_address(_strip_host_brackets(host)).version == 6
    except ValueError:
        return False


def split_host_port(value: str) -> tuple[str, str | None]:
    host_port = value.strip()
    if host_port.startswith("["):
        host_end = host_port.find("]")
        if host_end != -1:
            host = host_port[1:host_end]
            remainder = host_port[host_end + 1 :]
            if remainder.startswith(":"):
                return host, remainder[1:]
            return host, None

    if host_port.count(":") == 0:
        return host_port, None

    if host_port.count(":") == 1:
        host, port = host_port.rsplit(":", 1)
        return host, port

    host, port = host_port.rsplit(":", 1)
    if port.isdigit() and _is_ipv6_literal(host):
        return host, port

    if _is_ipv6_literal(host_port):
        return host_port, None

    return host_port, None


def format_url_netloc(host: str, port: int | str | None = None) -> str:
    normalized_host = _strip_host_brackets(host.strip())
    if _is_ipv6_literal(normalized_host):
        netloc = f"[{normalized_host}]"
    else:
        netloc = normalized_host
    if port is None:
        return netloc
    return f"{netloc}:{port}"


def _normalize_url_netloc(netloc: str) -> str:
    userinfo, separator, host_port = netloc.rpartition("@")
    host, port = split_host_port(host_port)
    normalized = format_url_netloc(host, port)
    if separator:
        return f"{userinfo}@{normalized}"
    return normalized


def normalize_rpc_url(address: str) -> str:
    normalized = address.strip()
    scheme = "http"
    rest = normalized
    if "://" in normalized:
        raw_scheme, rest = normalized.split("://", 1)
        raw_scheme = raw_scheme.lower()
        scheme = raw_scheme if raw_scheme in {"http", "https"} else "http"

    parts = urlsplit(f"{scheme}://{rest}")
    netloc = _normalize_url_netloc(parts.netloc) if parts.netloc else ""
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment)).rstrip("/")


def resolve_local_rpc_url(
    address: str,
    *,
    default_host: str = "127.0.0.1",
    default_ipv6_host: str | None = None,
    default_port: int = 26657,
) -> str:
    normalized = normalize_rpc_url(address)
    parts = urlsplit(normalized)
    host = parts.hostname or default_host
    local_host = None
    if host == "0.0.0.0":
        local_host = default_host
    elif host == "::":
        local_host = default_ipv6_host or default_host

    if local_host is not None:
        netloc = format_url_netloc(local_host, parts.port or default_port)
        return urlunsplit((parts.scheme or "http", netloc, parts.path, "", ""))
    return normalized


def load_tendermint_config(config: Constants):
    if not (config.COMETBFT_HOME.exists() and config.COMETBFT_HOME.is_dir()):
        raise FileNotFoundError("You must initialize CometBFT first")
    if not (config.COMETBFT_CONFIG.exists() and config.COMETBFT_CONFIG.is_file()):
        raise FileNotFoundError(f"File not found: {config.COMETBFT_CONFIG}")

    return load_toml(config.COMETBFT_CONFIG)


def load_xian_config(config: Constants):
    if not (config.COMETBFT_HOME.exists() and config.COMETBFT_HOME.is_dir()):
        raise FileNotFoundError("You must initialize CometBFT first")
    if not (config.XIAN_CONFIG.exists() and config.XIAN_CONFIG.is_file()):
        raise FileNotFoundError(f"File not found: {config.XIAN_CONFIG}")

    return load_toml(config.XIAN_CONFIG)


def load_genesis_data(config: Constants):
    if not (config.COMETBFT_GENESIS.exists() and config.COMETBFT_GENESIS.is_file()):
        raise FileNotFoundError(f"File not found: {config.COMETBFT_GENESIS}")

    with open(config.COMETBFT_GENESIS, "r") as file:
        return json.load(file)
