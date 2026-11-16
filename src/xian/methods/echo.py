from cometbft.abci.v1beta1.types_pb2 import ResponseEcho


async def echo(self, req) -> ResponseEcho:
    acknowledgment = self._evidence_history_pairing.register_echo(req.message)
    return ResponseEcho(message=req.message if acknowledgment is None else acknowledgment)
