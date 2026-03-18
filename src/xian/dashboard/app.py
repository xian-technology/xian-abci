from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from fnmatch import fnmatch
from pathlib import Path

import aiohttp
from aiohttp import web
from loguru import logger

STATIC_DIR = Path(__file__).parent / "static"

try:
    from xian_py.decompiler import ContractDecompiler

    _decompiler = ContractDecompiler()
except Exception:
    _decompiler = None


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
        tx = _decode_tx_bytes(raw_tx)
        if tx:
            payload = tx.get("payload", {})
            decoded_txs.append(
                {
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
        request.app["session"], request.app["rpc_url"], "status"
    )


async def handle_net_info(request: web.Request) -> web.Response:
    return await _proxy(
        request.app["session"], request.app["rpc_url"], "net_info"
    )


async def handle_validators(request: web.Request) -> web.Response:
    return await _proxy(
        request.app["session"], request.app["rpc_url"], "validators"
    )


async def handle_consensus(request: web.Request) -> web.Response:
    return await _proxy(
        request.app["session"],
        request.app["rpc_url"],
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
        request.app["rpc_url"],
        "blockchain",
        params or None,
    )


async def handle_block(request: web.Request) -> web.Response:
    height = request.match_info["height"]
    session = request.app["session"]
    rpc = request.app["rpc_url"]

    try:
        result = await _raw_rpc(session, rpc, "block", {"height": height})
        block = result.get("block", {})
        txs_raw = (block.get("data") or {}).get("txs") or []

        decoded_txs = []
        for raw_tx in txs_raw:
            tx = _decode_tx_bytes(raw_tx)
            entry = tx if tx else {"raw": raw_tx}
            # Compute the CometBFT tx hash (SHA-256 of raw bytes)
            try:
                raw_bytes = base64.b64decode(raw_tx)
                entry["tx_hash"] = hashlib.sha256(raw_bytes).hexdigest().upper()
            except Exception:
                pass
            decoded_txs.append(entry)

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
    rpc = request.app["rpc_url"]

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
    rpc = request.app["rpc_url"]

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
        request.app["rpc_url"],
        "unconfirmed_txs",
    )


async def handle_contract(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    session = request.app["session"]
    rpc = request.app["rpc_url"]

    try:
        code = await _abci_query(session, rpc, f"contract/{name}")
        methods = await _abci_query(session, rpc, f"contract_methods/{name}")
        variables = await _abci_query(session, rpc, f"contract_vars/{name}")

        source = None
        if code and _decompiler:
            try:
                source = _decompiler.decompile(code)
            except Exception:
                source = code
        elif code:
            source = code

        return web.json_response(
            {
                "name": name,
                "code": source,
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


async def handle_abci_query(
    request: web.Request,
) -> web.Response:
    query_path = request.match_info["path"]
    session = request.app["session"]
    rpc = request.app["rpc_url"]

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
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/net_info", handle_net_info)
    app.router.add_get("/api/validators", handle_validators)
    app.router.add_get("/api/consensus", handle_consensus)
    app.router.add_get("/api/blockchain", handle_blockchain)
    app.router.add_get("/api/block/{height}", handle_block)
    app.router.add_get("/api/block_results/{height}", handle_block_results)
    app.router.add_get("/api/tx/{hash}", handle_tx)
    app.router.add_get("/api/unconfirmed_txs", handle_unconfirmed)
    app.router.add_get("/api/contract/{name}", handle_contract)
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
