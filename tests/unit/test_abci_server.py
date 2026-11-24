"""Tests for the ABCI socket server and protocol dispatch."""

import asyncio
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from abci.server import ABCIServer, ProtocolHandler, _stop
from abci.utils import encode_varint, read_message, read_messages, write_message
from tendermint.abci.types_pb2 import (
    Request,
    Response,
    ResponseApplySnapshotChunk,
    ResponseEcho,
    ResponseListSnapshots,
    ResponseLoadSnapshotChunk,
    ResponseOfferSnapshot,
)


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


class StreamFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_fragmented_length_and_payload_then_the_next_frame(self):
        request = Request()
        request.echo.message = "x" * 200_000
        frame = write_message(request)
        reader = asyncio.StreamReader()
        pending = asyncio.create_task(read_message(reader, Request))
        for chunk in (frame[:1], frame[1:2], frame[2:70_000]):
            reader.feed_data(chunk)
            await asyncio.sleep(0)
            self.assertFalse(pending.done())

        # The following zero-length request is a valid empty protobuf.
        reader.feed_data(frame[70_000:] + write_message(Request()))
        self.assertEqual(await pending, request)
        self.assertEqual(await read_message(reader, Request), Request())

    async def test_rejects_invalid_length_without_waiting_for_a_payload(self):
        for prefix in (b"\x80" * 10, encode_varint(2**31)):
            with self.subTest(prefix=prefix):
                reader = asyncio.StreamReader()
                reader.feed_data(prefix)
                with self.assertRaisesRegex(ValueError, "ABCI frame length"):
                    await asyncio.wait_for(read_message(reader, Request), timeout=1)

    async def test_largest_allowed_length_is_read_as_a_payload(self):
        length = 2**31 - 1
        reader = asyncio.StreamReader()
        reader.feed_data(encode_varint(length))
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError) as incomplete:
            await read_message(reader, Request)
        self.assertEqual(incomplete.exception.expected, length)


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
                    echo_request.echo.message = "ping" * 40_000
                    frame = write_message(echo_request)

                    # Split the length prefix of a frame larger than the stream's
                    # default read limit.
                    writer.write(frame[:1])
                    await writer.drain()
                    await asyncio.sleep(0.05)
                    writer.write(frame[1:])
                    await writer.drain()

                    response = await self._read_response(reader)
                    self.assertEqual(response.echo.message, "pong")

                    # Queue a second large Echo and a Flush in the same write.
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

    async def test_host_failure_closes_connection_and_stops_all_abci_work(self):
        app = AsyncMock()
        app.echo.side_effect = OSError("injected host failure")
        server = ABCIServer(app)
        reader = asyncio.StreamReader()
        request = Request()
        request.echo.message = "ping"
        reader.feed_data(write_message(request))
        reader.feed_eof()
        writer = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch("abci.server._stop", new_callable=AsyncMock) as stop:
            with self.assertRaisesRegex(OSError, "injected host failure"):
                await server._handler(reader, writer)

        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()
        stop.assert_awaited_once()

    async def test_truncated_request_closes_connection_without_dispatch(self):
        request = Request()
        request.echo.message = "ping"
        for partial in (b"\x80", write_message(request)[:-1]):
            with self.subTest(partial=partial):
                app = AsyncMock()
                reader = asyncio.StreamReader()
                reader.feed_data(partial)
                reader.feed_eof()
                writer = MagicMock()
                writer.wait_closed = AsyncMock()
                with patch("abci.server._stop", new_callable=AsyncMock) as stop:
                    await ABCIServer(app)._handler(reader, writer)

                app.echo.assert_not_awaited()
                writer.write.assert_not_called()
                writer.wait_closed.assert_awaited_once()
                stop.assert_awaited_once()

    async def test_shutdown_cancellation_does_not_start_recursive_global_shutdown(self):
        server = ABCIServer(AsyncMock())
        reader = asyncio.StreamReader()
        writer = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch("abci.server._stop", new_callable=AsyncMock) as stop:
            connection = asyncio.create_task(server._handler(reader, writer))
            await asyncio.sleep(0)
            connection.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await connection

        writer.close.assert_called_once()
        stop.assert_not_awaited()

    async def test_cancellation_while_closing_does_not_restart_global_shutdown(self):
        server = ABCIServer(AsyncMock())
        reader = asyncio.StreamReader()
        reader.feed_eof()
        writer = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=asyncio.CancelledError())

        with patch("abci.server._stop", new_callable=AsyncMock) as stop:
            with self.assertRaises(asyncio.CancelledError):
                await server._handler(reader, writer)

        stop.assert_not_awaited()


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
