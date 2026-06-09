"""Tests for the ABCI socket server and protocol dispatch."""

import asyncio
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from cometbft.abci.v1beta1.types_pb2 import (
    ResponseApplySnapshotChunk,
    ResponseEcho,
    ResponseListSnapshots,
    ResponseLoadSnapshotChunk,
    ResponseOfferSnapshot,
)
from cometbft.abci.v1beta3.types_pb2 import Request, Response

from abci.server import ABCIServer, ProtocolHandler, _stop
from abci.utils import read_messages, write_message


def _parse_single_response(payload: bytes) -> Response:
    messages = list(read_messages(BytesIO(payload), Response))
    assert len(messages) == 1
    return messages[0]


class ProtocolHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_echo_dispatches_to_app(self):
        app = AsyncMock()
        app.echo.return_value = ResponseEcho(message="pong")
        handler = ProtocolHandler(app)

        request = Request()
        request.echo.message = "ping"
        payload = await handler.process("echo", request)

        response = _parse_single_response(payload)
        self.assertEqual(response.echo.message, "pong")
        app.echo.assert_awaited_once()

    async def test_flush_is_answered_without_app_involvement(self):
        app = AsyncMock()
        handler = ProtocolHandler(app)

        request = Request()
        request.flush.SetInParent()
        payload = await handler.process("flush", request)

        response = _parse_single_response(payload)
        self.assertEqual(response.WhichOneof("value"), "flush")

    async def test_dispatches_snapshot_requests(self):
        app = AsyncMock()
        app.list_snapshots.return_value = ResponseListSnapshots()
        app.offer_snapshot.return_value = ResponseOfferSnapshot()
        app.load_snapshot_chunk.return_value = ResponseLoadSnapshotChunk(chunk=b"abc")
        app.apply_snapshot_chunk.return_value = ResponseApplySnapshotChunk()
        handler = ProtocolHandler(app)

        for req_type in (
            "list_snapshots",
            "offer_snapshot",
            "load_snapshot_chunk",
            "apply_snapshot_chunk",
        ):
            request = Request()
            getattr(request, req_type).SetInParent()

            payload = await handler.process(req_type, request)

            response = _parse_single_response(payload)
            self.assertEqual(response.WhichOneof("value"), req_type)

    async def test_unknown_request_type_returns_exception_response(self):
        handler = ProtocolHandler(AsyncMock())

        payload = await handler.process("bogus", Request())

        response = _parse_single_response(payload)
        self.assertEqual(response.exception.error, "ABCI request not found")


class AbciServerHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def _read_response(self, reader: asyncio.StreamReader) -> Response:
        # Responses in these tests are tiny, so the length varint is one byte.
        length = (await reader.readexactly(1))[0]
        assert length < 0x80
        body = await reader.readexactly(length)
        response = Response()
        response.ParseFromString(body)
        return response

    async def test_unix_socket_round_trip_handles_partial_and_batched_frames(self):
        app = AsyncMock()
        app.echo.return_value = ResponseEcho(message="pong")

        with TemporaryDirectory() as tmp:
            socket_path = str(Path(tmp) / "abci.sock")
            abci_server = ABCIServer(app, socket_path=socket_path)

            with patch("abci.server._stop", new_callable=AsyncMock) as stop:
                server = await asyncio.start_unix_server(
                    abci_server._handler,
                    path=socket_path,
                )
                try:
                    reader, writer = await asyncio.open_unix_connection(socket_path)

                    echo_request = Request()
                    echo_request.echo.message = "ping"
                    frame = write_message(echo_request)

                    # Deliver the frame in two chunks to exercise buffering of
                    # partial messages.
                    writer.write(frame[:1])
                    await writer.drain()
                    await asyncio.sleep(0.05)
                    writer.write(frame[1:])
                    await writer.drain()

                    response = await self._read_response(reader)
                    self.assertEqual(response.echo.message, "pong")

                    # Deliver two complete frames in one chunk to exercise the
                    # multi-message parsing loop.
                    flush_request = Request()
                    flush_request.flush.SetInParent()
                    writer.write(frame + write_message(flush_request))
                    await writer.drain()

                    response = await self._read_response(reader)
                    self.assertEqual(response.echo.message, "pong")
                    response = await self._read_response(reader)
                    self.assertEqual(response.WhichOneof("value"), "flush")

                    writer.close()
                    await writer.wait_closed()

                    # The handler reacts to the disconnect by requesting
                    # shutdown.
                    for _ in range(100):
                        if stop.await_count:
                            break
                        await asyncio.sleep(0.01)
                    stop.assert_awaited_once()
                finally:
                    server.close()
                    await server.wait_closed()


class StopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_outstanding_tasks(self):
        started = asyncio.Event()

        async def _hang():
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(_hang())
        await started.wait()

        await _stop()

        self.assertTrue(task.cancelled())
