from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from loguru import logger

STATIC_DIR = Path(__file__).parent / "static"
LOCALNET_PORT_STRIDE = 100


def normalize_rpc_url(address: str) -> str:
    if address.startswith(("http://", "https://")):
        return address.rstrip("/")
    addr = address.replace("tcp://", "").replace("unix://", "")
    return f"http://{addr.rstrip('/')}"


def _build_ws_url(rpc_url: str) -> str:
    if rpc_url.startswith("https://"):
        return f"wss://{rpc_url.removeprefix('https://')}/websocket"
    return f"ws://{rpc_url.removeprefix('http://')}/websocket"


def _decode_b64(val: str) -> str:
    return base64.b64decode(val).decode("utf-8")


def _decode_tx_bytes(b64: str) -> dict | None:
    """Decode a CometBFT base64 tx → hex → JSON → dict."""
    try:
        hex_str = _decode_b64(b64)
        json_str = bytes.fromhex(hex_str).decode("utf-8")
        return json.loads(json_str)
    except Exception:
        return None


def _decode_block_tx_entry(b64: str) -> dict:
    """Decode a block tx and attach the canonical tx hash when possible."""
    entry = _decode_tx_bytes(b64) or {"raw": b64}
    try:
        raw_bytes = base64.b64decode(b64)
        entry["tx_hash"] = hashlib.sha256(raw_bytes).hexdigest().upper()
    except Exception:
        pass
    return entry


def _decode_abci_value(b64: str) -> str | dict | list | None:
    """Decode a base64 ABCI response value."""
    try:
        raw = _decode_b64(b64)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    except Exception:
        return None


def _normalize_peer_rpc_url(peer: dict) -> str | None:
    node_info = peer.get("node_info", {}) if isinstance(peer, dict) else {}
    other = node_info.get("other", {}) if isinstance(node_info, dict) else {}
    rpc_address = other.get("rpc_address")
    if not isinstance(rpc_address, str) or not rpc_address.strip():
        return None

    normalized = normalize_rpc_url(rpc_address)
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    if host in {"0.0.0.0", "::", "127.0.0.1", "localhost"}:
        remote_ip = peer.get("remote_ip")
        if isinstance(remote_ip, str) and remote_ip.strip():
            host = remote_ip.strip()

    if not host:
        return None

    port = parsed.port
    if port is None:
        return None

    return urlunsplit((parsed.scheme or "http", f"{host}:{port}", "", "", ""))


def _node_index_from_moniker(moniker: str | None) -> int | None:
    if not isinstance(moniker, str):
        return None
    match = re.fullmatch(r"node-(\d+)", moniker.strip())
    if match is None:
        return None
    return int(match.group(1))


def _loopback_host(host: str | None) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _localnet_rpc_variants(
    base_rpc_url: str,
    current_moniker: str | None,
    peer_moniker: str | None,
) -> set[str]:
    parsed = urlsplit(base_rpc_url)
    base_host = parsed.hostname
    base_port = parsed.port
    current_index = _node_index_from_moniker(current_moniker)
    peer_index = _node_index_from_moniker(peer_moniker)

    if (
        not _loopback_host(base_host)
        or base_port is None
        or current_index is None
        or peer_index is None
    ):
        return set()

    port = base_port + (peer_index - current_index) * LOCALNET_PORT_STRIDE
    if port <= 0:
        return set()

    scheme = parsed.scheme or "http"
    return {
        urlunsplit((scheme, f"{host}:{port}", "", "", ""))
        for host in ("127.0.0.1", "localhost")
    }


async def _allowed_rpc_urls(
    session: aiohttp.ClientSession,
    rpc_url: str,
) -> set[str]:
    allowed = {rpc_url}
    current_moniker = None
    try:
        status = await _raw_rpc(session, rpc_url, "status")
        net_info = await _raw_rpc(session, rpc_url, "net_info")
    except Exception:
        return allowed

    current_moniker = (
        status.get("node_info", {}).get("moniker")
        if isinstance(status, dict)
        else None
    )

    for peer in net_info.get("peers", []) or []:
        peer_rpc_url = _normalize_peer_rpc_url(peer)
        if peer_rpc_url:
            allowed.add(peer_rpc_url)
        peer_moniker = (
            peer.get("node_info", {}).get("moniker")
            if isinstance(peer, dict)
            else None
        )
        allowed.update(
            _localnet_rpc_variants(rpc_url, current_moniker, peer_moniker)
        )

    return allowed


async def _request_rpc_url(request: web.Request) -> str:
    default_rpc_url = request.app["rpc_url"]
    requested_rpc_url = request.query.get("rpc")
    if not requested_rpc_url:
        return default_rpc_url

    normalized_requested_rpc_url = normalize_rpc_url(requested_rpc_url)
    if normalized_requested_rpc_url == default_rpc_url:
        return default_rpc_url

    allowed_rpc_urls = await _allowed_rpc_urls(
        request.app["session"], default_rpc_url
    )
    if normalized_requested_rpc_url not in allowed_rpc_urls:
        raise web.HTTPBadRequest(
            text=f"Unsupported rpc target: {normalized_requested_rpc_url}"
        )

    return normalized_requested_rpc_url


def _request_int(request: web.Request, key: str, default: int) -> int:
    raw_value = request.query.get(key)
    if raw_value is None:
        return default
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed_value)


async def _raw_rpc(
    session: aiohttp.ClientSession,
    rpc_url: str,
    path: str,
    params: dict | None = None,
) -> dict:
    url = f"{rpc_url}/{path}"
    async with session.get(
        url,
        params=params,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        return data.get("result", data)


async def _proxy(
    session: aiohttp.ClientSession,
    rpc_url: str,
    path: str,
    params: dict | None = None,
) -> web.Response:
    try:
        result = await _raw_rpc(session, rpc_url, path, params)
        return web.json_response(result)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"}, status=502
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _abci_query(
    session: aiohttp.ClientSession,
    rpc_url: str,
    query_path: str,
) -> str | dict | list | None:
    """Run an ABCI query and return the decoded value."""
    result = await _raw_rpc(
        session,
        rpc_url,
        "abci_query",
        {"path": f'"/{query_path}"'},
    )
    response = result.get("response", {})
    val = response.get("value")
    if not val:
        return None
    return _decode_abci_value(val)


# ── Subscription manager ──────────────────────────────────────


class SubscriptionManager:
    """Track per-client subscriptions for state changes and events.

    Clients send JSON messages to subscribe/unsubscribe:

        {"action": "subscribe", "type": "state", "key": "currency.balances:alice"}
        {"action": "subscribe", "type": "state", "key": "currency.balances:*"}
        {"action": "subscribe", "type": "event", "contract": "currency", "event": "Transfer"}
        {"action": "subscribe", "type": "event", "contract": "*"}
        {"action": "unsubscribe", "type": "state", "key": "currency.balances:alice"}
        {"action": "unsubscribe_all"}
        {"action": "list"}

    State subscriptions support glob patterns (fnmatch) on the key.
    Event subscriptions match on contract and optionally event name.
    """

    def __init__(self):
        # ws -> set of state key patterns
        self._state_subs: dict[web.WebSocketResponse, set[str]] = {}
        # ws -> list of {"contract": pattern, "event": pattern | None}
        self._event_subs: dict[web.WebSocketResponse, list[dict]] = {}

    def add_client(self, ws: web.WebSocketResponse) -> None:
        self._state_subs[ws] = set()
        self._event_subs[ws] = []

    def remove_client(self, ws: web.WebSocketResponse) -> None:
        self._state_subs.pop(ws, None)
        self._event_subs.pop(ws, None)

    def handle_message(self, ws: web.WebSocketResponse, data: dict) -> dict:
        """Process a subscription message and return a response dict."""
        action = data.get("action")

        if action == "subscribe":
            return self._subscribe(ws, data)
        elif action == "unsubscribe":
            return self._unsubscribe(ws, data)
        elif action == "unsubscribe_all":
            self._state_subs.get(ws, set()).clear()
            if ws in self._event_subs:
                self._event_subs[ws].clear()
            return {"status": "ok", "action": "unsubscribe_all"}
        elif action == "list":
            return {
                "status": "ok",
                "action": "list",
                "state": sorted(self._state_subs.get(ws, set())),
                "events": self._event_subs.get(ws, []),
            }
        else:
            return {"status": "error", "message": f"unknown action: {action}"}

    def _subscribe(self, ws: web.WebSocketResponse, data: dict) -> dict:
        sub_type = data.get("type")
        if sub_type == "state":
            key = data.get("key", "")
            if not key:
                return {"status": "error", "message": "missing key"}
            self._state_subs.setdefault(ws, set()).add(key)
            return {
                "status": "ok",
                "action": "subscribe",
                "type": "state",
                "key": key,
            }
        elif sub_type == "event":
            contract = data.get("contract", "*")
            event = data.get("event")
            entry = {"contract": contract, "event": event}
            subs = self._event_subs.setdefault(ws, [])
            if entry not in subs:
                subs.append(entry)
            return {
                "status": "ok",
                "action": "subscribe",
                "type": "event",
                **entry,
            }
        else:
            return {"status": "error", "message": f"unknown type: {sub_type}"}

    def _unsubscribe(self, ws: web.WebSocketResponse, data: dict) -> dict:
        sub_type = data.get("type")
        if sub_type == "state":
            key = data.get("key", "")
            self._state_subs.get(ws, set()).discard(key)
            return {
                "status": "ok",
                "action": "unsubscribe",
                "type": "state",
                "key": key,
            }
        elif sub_type == "event":
            contract = data.get("contract", "*")
            event = data.get("event")
            entry = {"contract": contract, "event": event}
            subs = self._event_subs.get(ws, [])
            if entry in subs:
                subs.remove(entry)
            return {
                "status": "ok",
                "action": "unsubscribe",
                "type": "event",
                **entry,
            }
        else:
            return {"status": "error", "message": f"unknown type: {sub_type}"}

    def match_state(self, key: str) -> list[web.WebSocketResponse]:
        """Return all WS clients subscribed to this state key."""
        matched = []
        for ws, patterns in self._state_subs.items():
            for pattern in patterns:
                if fnmatch(key, pattern):
                    matched.append(ws)
                    break
        return matched

    def match_event(
        self, contract: str, event: str
    ) -> list[web.WebSocketResponse]:
        """Return all WS clients subscribed to this contract event."""
        matched = []
        for ws, subs in self._event_subs.items():
            for sub in subs:
                if fnmatch(contract, sub["contract"]) and (
                    sub["event"] is None or fnmatch(event, sub["event"])
                ):
                    matched.append(ws)
                    break
        return matched


# ── WebSocket: CometBFT subscriber ────────────────────────────

_SUBSCRIBE_NEW_BLOCK = json.dumps(
    {
        "jsonrpc": "2.0",
        "method": "subscribe",
        "id": 0,
        "params": {"query": "tm.event='NewBlock'"},
    }
)

_SUBSCRIBE_TX = json.dumps(
    {
        "jsonrpc": "2.0",
        "method": "subscribe",
        "id": 1,
        "params": {"query": "tm.event='Tx'"},
    }
)

_RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30]


async def _cometbft_subscriber(app: web.Application) -> None:
    """Maintain a persistent WS to CometBFT and fan out events."""
    ws_url = app["ws_url"]
    session = app["session"]
    delay_idx = 0

    while True:
        try:
            logger.info(f"Connecting to CometBFT WebSocket: {ws_url}")
            async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                delay_idx = 0
                logger.info(
                    "CometBFT WebSocket connected, subscribing to"
                    " NewBlock and Tx"
                )
                await _broadcast_status(app, "online")
                await ws.send_str(_SUBSCRIBE_NEW_BLOCK)
                await ws.send_str(_SUBSCRIBE_TX)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            await _handle_cometbft_event(app, data)
                        except Exception:
                            logger.exception(
                                "Error handling CometBFT WS message"
                            )
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning(f"CometBFT WS error: {ws.exception()}")
                        break
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        break

        except asyncio.CancelledError:
            logger.info("CometBFT subscriber task cancelled")
            return
        except Exception:
            logger.exception("CometBFT WS connection failed")

        await _broadcast_status(app, "offline")
        delay = _RECONNECT_DELAYS[min(delay_idx, len(_RECONNECT_DELAYS) - 1)]
        delay_idx += 1
        logger.info(f"Reconnecting to CometBFT WS in {delay}s...")
        await asyncio.sleep(delay)


async def _handle_cometbft_event(app: web.Application, data: dict) -> None:
    """Parse CometBFT events and route to subscribers."""
    result = data.get("result", {})
    event_data = result.get("data", {})
    event_type = event_data.get("type")

    if event_type == "tendermint/event/NewBlock":
        await _handle_new_block(app, event_data)
    elif event_type == "tendermint/event/Tx":
        await _handle_tx_event(app, event_data)


async def _handle_new_block(app: web.Application, event_data: dict) -> None:
    """Broadcast new block summary to all connected clients."""
    value = event_data.get("value", {})
    block = value.get("block", {})
    header = block.get("header", {})
    txs_raw = (block.get("data") or {}).get("txs") or []

    decoded_txs = []
    for raw_tx in txs_raw:
        tx = _decode_block_tx_entry(raw_tx)
        payload = tx.get("payload", {})
        if payload:
            payload = tx.get("payload", {})
            decoded_txs.append(
                {
                    "tx_hash": tx.get("tx_hash"),
                    "contract": payload.get("contract"),
                    "function": payload.get("function"),
                    "sender": payload.get("sender"),
                    "stamps_supplied": payload.get("stamps_supplied"),
                }
            )

    block_id = value.get("block_id", {})

    message = json.dumps(
        {
            "type": "new_block",
            "height": int(header.get("height", 0)),
            "hash": block_id.get("hash", ""),
            "time": header.get("time", ""),
            "num_txs": len(txs_raw),
            "proposer": header.get("proposer_address", ""),
            "txs": decoded_txs,
        }
    )

    await _broadcast(app, message)


async def _handle_tx_event(app: web.Application, event_data: dict) -> None:
    """Route per-tx state changes and contract events to subscribers."""
    subs: SubscriptionManager = app["subscriptions"]
    value = event_data.get("value", {})
    tx_result = value.get("TxResult", {})
    result = tx_result.get("result", {})

    # Parse ABCI events from the tx result
    events = result.get("events", [])

    for event in events:
        event_type = event.get("type", "")
        attributes = event.get("attributes", [])

        if event_type == "StateChange":
            # Route state changes to subscribers
            for attr in attributes:
                raw_key = attr.get("key", "")
                raw_value = attr.get("value", "")

                # Decode base64 if needed (CometBFT encodes attributes)
                try:
                    raw_key = base64.b64decode(raw_key).decode()
                except Exception:
                    pass
                try:
                    raw_value = base64.b64decode(raw_value).decode()
                except Exception:
                    pass

                # Reverse the key translation: __ → : then _ → .
                state_key = raw_key.replace("__", ":").replace("_", ".")

                matched = subs.match_state(state_key)
                if matched:
                    msg = json.dumps(
                        {
                            "type": "state_change",
                            "key": state_key,
                            "value": raw_value,
                        }
                    )
                    for ws in matched:
                        try:
                            await ws.send_str(msg)
                        except Exception:
                            pass
        else:
            # Contract event — extract attributes
            attrs = {}
            for attr in attributes:
                k = attr.get("key", "")
                v = attr.get("value", "")
                try:
                    k = base64.b64decode(k).decode()
                except Exception:
                    pass
                try:
                    v = base64.b64decode(v).decode()
                except Exception:
                    pass
                attrs[k] = v

            contract = attrs.get("contract", "")

            try:
                event_name = base64.b64decode(event_type).decode()
            except Exception:
                event_name = event_type

            matched = subs.match_event(contract, event_name)
            if matched:
                msg = json.dumps(
                    {
                        "type": "contract_event",
                        "event": event_name,
                        "contract": contract,
                        "data": {
                            k: v
                            for k, v in attrs.items()
                            if k not in ("contract", "signer", "caller")
                        },
                        "signer": attrs.get("signer", ""),
                        "caller": attrs.get("caller", ""),
                    }
                )
                for ws in matched:
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        pass


async def _broadcast(app: web.Application, message: str) -> None:
    """Send a message to all connected browser WS clients."""
    closed = []
    for ws in set(app["ws_clients"]):
        try:
            await ws.send_str(message)
        except Exception:
            closed.append(ws)
    for ws in closed:
        app["ws_clients"].discard(ws)


async def _broadcast_status(app: web.Application, status: str) -> None:
    """Notify browsers about CometBFT connection status."""
    await _broadcast(app, json.dumps({"type": "node_status", "status": status}))


# ── WebSocket: browser handler ─────────────────────────────────


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25.0)
    await ws.prepare(request)

    subs: SubscriptionManager = request.app["subscriptions"]
    request.app["ws_clients"].add(ws)
    subs.add_client(ws)
    logger.debug(
        f"Browser WS connected ({len(request.app['ws_clients'])} total)"
    )

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if "action" in data:
                        response = subs.handle_message(ws, data)
                        await ws.send_str(json.dumps(response))
                except json.JSONDecodeError:
                    await ws.send_str(
                        json.dumps(
                            {"status": "error", "message": "invalid JSON"}
                        )
                    )
                except Exception as exc:
                    await ws.send_str(
                        json.dumps({"status": "error", "message": str(exc)})
                    )
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        subs.remove_client(ws)
        request.app["ws_clients"].discard(ws)
        logger.debug(
            f"Browser WS disconnected ({len(request.app['ws_clients'])} total)"
        )

    return ws


# ── route handlers ──────────────────────────────────────────────


async def handle_index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_status(request: web.Request) -> web.Response:
    return await _proxy(
        request.app["session"],
        await _request_rpc_url(request),
        "status",
    )


async def handle_config(request: web.Request) -> web.Response:
    return web.json_response({"default_rpc_url": request.app["rpc_url"]})


async def handle_net_info(request: web.Request) -> web.Response:
    return await _proxy(
        request.app["session"],
        await _request_rpc_url(request),
        "net_info",
    )


async def handle_validators(request: web.Request) -> web.Response:
    return await _proxy(
        request.app["session"],
        await _request_rpc_url(request),
        "validators",
    )


async def handle_consensus(request: web.Request) -> web.Response:
    return await _proxy(
        request.app["session"],
        await _request_rpc_url(request),
        "consensus_state",
    )


async def handle_blockchain(request: web.Request) -> web.Response:
    params = {}
    if "min_height" in request.query:
        params["minHeight"] = request.query["min_height"]
    if "max_height" in request.query:
        params["maxHeight"] = request.query["max_height"]
    return await _proxy(
        request.app["session"],
        await _request_rpc_url(request),
        "blockchain",
        params or None,
    )


async def handle_block(request: web.Request) -> web.Response:
    height = request.match_info["height"]
    session = request.app["session"]
    rpc = await _request_rpc_url(request)

    try:
        result = await _raw_rpc(session, rpc, "block", {"height": height})
        block = result.get("block", {})
        txs_raw = (block.get("data") or {}).get("txs") or []

        decoded_txs = [_decode_block_tx_entry(raw_tx) for raw_tx in txs_raw]

        result["decoded_txs"] = decoded_txs
        return web.json_response(result)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_block_results(
    request: web.Request,
) -> web.Response:
    height = request.match_info["height"]
    session = request.app["session"]
    rpc = await _request_rpc_url(request)

    try:
        result = await _raw_rpc(
            session, rpc, "block_results", {"height": height}
        )
        for tx_res in result.get("txs_results") or []:
            if tx_res.get("data"):
                decoded = _decode_abci_value(tx_res["data"])
                if decoded is not None:
                    tx_res["data_decoded"] = decoded
        return web.json_response(result)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_tx(request: web.Request) -> web.Response:
    tx_hash = request.match_info["hash"]
    if not tx_hash.startswith("0x"):
        tx_hash = f"0x{tx_hash}"
    session = request.app["session"]
    rpc = await _request_rpc_url(request)

    try:
        result = await _raw_rpc(session, rpc, "tx", {"hash": tx_hash})

        if result.get("tx"):
            decoded = _decode_tx_bytes(result["tx"])
            if decoded:
                result["tx_decoded"] = decoded

        tx_result = result.get("tx_result", {})
        if tx_result.get("data"):
            decoded = _decode_abci_value(tx_result["data"])
            if decoded is not None:
                tx_result["data_decoded"] = decoded

        return web.json_response(result)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_unconfirmed(
    request: web.Request,
) -> web.Response:
    return await _proxy(
        request.app["session"],
        await _request_rpc_url(request),
        "unconfirmed_txs",
    )


async def handle_contract(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    session = request.app["session"]
    rpc = await _request_rpc_url(request)

    try:
        code = await _abci_query(session, rpc, f"contract_source/{name}")
        if code is None:
            code = await _abci_query(session, rpc, f"contract/{name}")
        methods = await _abci_query(session, rpc, f"contract_methods/{name}")
        variables = await _abci_query(session, rpc, f"contract_vars/{name}")
        metadata = await _abci_query(session, rpc, f"contract_info/{name}")
        summary = await _abci_query(session, rpc, f"contract_summary/{name}")

        return web.json_response(
            {
                "name": name,
                "code": code,
                "metadata": metadata or {},
                "summary": summary,
                "methods": (
                    methods.get("methods", methods)
                    if isinstance(methods, dict)
                    else methods
                ),
                "variables": variables,
            }
        )
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_address(request: web.Request) -> web.Response:
    address = request.match_info["address"]
    session = request.app["session"]
    rpc = await _request_rpc_url(request)
    limit = min(_request_int(request, "limit", 50), 500)
    offset = _request_int(request, "offset", 0)

    try:
        transactions = await _abci_query(
            session,
            rpc,
            f"txs_by_sender/{address}/limit={limit}/offset={offset}",
        )
        rewards = await _abci_query(
            session, rpc, f"developer_rewards/{address}"
        )
        available = isinstance(transactions, list)

        return web.json_response(
            {
                "address": address,
                "available": available,
                "transactions": transactions if available else [],
                "limit": limit,
                "offset": offset,
                "has_more": bool(
                    available and len(transactions) >= limit and limit > 0
                ),
                "developer_rewards": rewards
                if isinstance(rewards, dict)
                else None,
            }
        )
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_contracts(request: web.Request) -> web.Response:
    session = request.app["session"]
    rpc = await _request_rpc_url(request)
    limit = min(_request_int(request, "limit", 100), 500)
    offset = _request_int(request, "offset", 0)
    sort = request.query.get("sort", "submitted_at")
    order = request.query.get("order", "desc")

    try:
        payload = await _abci_query(
            session,
            rpc,
            f"contracts/limit={limit}/offset={offset}/sort={sort}/order={order}",
        )
        if not isinstance(payload, dict):
            payload = {
                "items": payload or [],
                "limit": limit,
                "offset": offset,
                "sort": sort,
                "order": order,
            }
        return web.json_response(payload)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_recent_events(request: web.Request) -> web.Response:
    session = request.app["session"]
    rpc = await _request_rpc_url(request)
    limit = min(_request_int(request, "limit", 50), 500)
    offset = _request_int(request, "offset", 0)

    try:
        payload = await _abci_query(
            session,
            rpc,
            f"recent_events/limit={limit}/offset={offset}",
        )
        if not isinstance(payload, dict):
            payload = {
                "available": False,
                "items": [],
                "limit": limit,
                "offset": offset,
            }
        return web.json_response(payload)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_abci_query(
    request: web.Request,
) -> web.Response:
    query_path = request.match_info["path"]
    session = request.app["session"]
    rpc = await _request_rpc_url(request)

    try:
        result = await _raw_rpc(
            session,
            rpc,
            "abci_query",
            {"path": f'"/{query_path}"'},
        )

        response = result.get("response", {})
        if response.get("value"):
            decoded = _decode_abci_value(response["value"])
            if decoded is not None:
                response["value"] = decoded

        if response.get("key"):
            try:
                response["key"] = _decode_b64(response["key"])
            except Exception:
                pass

        return web.json_response(result)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"CometBFT RPC unavailable: {exc}"},
            status=502,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_monitoring(request: web.Request) -> web.Response:
    session = request.app["session"]
    rpc = await _request_rpc_url(request)

    async def fetch_decoded_query(path: str) -> dict:
        result = await _raw_rpc(
            session,
            rpc,
            "abci_query",
            {"path": f'"/{path}"'},
        )
        response = result.get("response", {})
        decoded = None
        if response.get("value"):
            decoded = _decode_abci_value(response["value"])
        return {"code": response.get("code"), "value": decoded}

    async def fetch_perf() -> dict:
        try:
            perf_query = await fetch_decoded_query("perf_status")
            if perf_query["code"] != 0 or perf_query["value"] is None:
                return {"enabled": False}
            snapshot = perf_query["value"]
            return {
                "enabled": bool(snapshot.get("enabled", False)),
                "snapshot": snapshot,
            }
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    async def fetch_bds() -> dict:
        try:
            bds_query = await fetch_decoded_query("bds_status")
            if bds_query["code"] != 0 or not isinstance(
                bds_query["value"], dict
            ):
                return {"enabled": False}
            return {"enabled": True, "status": bds_query["value"]}
        except Exception:
            return {"enabled": False}

    async def fetch_unconfirmed() -> dict:
        try:
            return await _raw_rpc(session, rpc, "unconfirmed_txs")
        except Exception as exc:
            return {"error": str(exc)}

    perf, bds, unconfirmed = await asyncio.gather(
        fetch_perf(),
        fetch_bds(),
        fetch_unconfirmed(),
    )
    return web.json_response(
        {
            "perf": perf,
            "bds": bds,
            "unconfirmed_txs": unconfirmed,
        }
    )


# ── app lifecycle ───────────────────────────────────────────────


async def _on_startup(app: web.Application) -> None:
    app["session"] = aiohttp.ClientSession()
    app["ws_clients"] = set()
    app["subscriptions"] = SubscriptionManager()
    app["_subscriber_task"] = asyncio.create_task(_cometbft_subscriber(app))


async def _on_cleanup(app: web.Application) -> None:
    task = app.get("_subscriber_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for ws in set(app.get("ws_clients", set())):
        await ws.close()

    await app["session"].close()


def create_app(
    cometbft_rpc_url: str = "http://127.0.0.1:26657",
) -> web.Application:
    normalized_rpc_url = normalize_rpc_url(cometbft_rpc_url)
    app = web.Application()
    app["rpc_url"] = normalized_rpc_url
    app["ws_url"] = _build_ws_url(normalized_rpc_url)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/explorer", handle_index)
    app.router.add_get("/explorer/{_path:.+}", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/net_info", handle_net_info)
    app.router.add_get("/api/validators", handle_validators)
    app.router.add_get("/api/consensus", handle_consensus)
    app.router.add_get("/api/blockchain", handle_blockchain)
    app.router.add_get("/api/block/{height}", handle_block)
    app.router.add_get("/api/block_results/{height}", handle_block_results)
    app.router.add_get("/api/tx/{hash}", handle_tx)
    app.router.add_get("/api/unconfirmed_txs", handle_unconfirmed)
    app.router.add_get("/api/monitoring", handle_monitoring)
    app.router.add_get("/api/contract/{name}", handle_contract)
    app.router.add_get("/api/address/{address}", handle_address)
    app.router.add_get("/api/contracts", handle_contracts)
    app.router.add_get("/api/recent_events", handle_recent_events)
    app.router.add_get("/api/abci_query/{path:.+}", handle_abci_query)

    return app


async def start_dashboard(
    host: str = "127.0.0.1",
    port: int = 8080,
    cometbft_rpc_url: str = "http://127.0.0.1:26657",
) -> web.AppRunner:
    app = create_app(cometbft_rpc_url)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"Dashboard running on http://{host}:{port}")
    return runner
