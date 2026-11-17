"""FinalizeBlock records validator liveness from the decided last commit and
jails a validator absent for the downtime window."""

import hashlib
import unittest
from io import BytesIO
from unittest import mock

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from abci.server import ProtocolHandler
from abci.utils import read_messages
from tendermint.abci.types_pb2 import (
    CommitInfo,
    Request,
    RequestCommit,
    RequestFinalizeBlock,
    Response,
    Validator,
    VoteInfo,
)
from tendermint.types.validator_pb2 import BlockIDFlag
from xian.config_paths import resolve_contracts_dir
from xian.xian_abci import Xian

NODES = ["11" * 32, "22" * 32, "33" * 32, "44" * 32]


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


def validator_address(validator_key: str) -> bytes:
    return hashlib.sha256(bytes.fromhex(validator_key)).digest()[:20]


def vote(validator_key: str, flag) -> VoteInfo:
    return VoteInfo(
        validator=Validator(address=validator_address(validator_key), power=10),
        block_id_flag=flag,
    )


@pytest.mark.usefixtures("node_fixture")
class ValidatorLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await Xian.create(constants=self.constants)
        self.addCleanup(self.app.client.close)
        self.addAsyncCleanup(self.app.close)
        self.handler = ProtocolHandler(self.app)
        self.driver = self.app.client.raw_driver
        contracts_dir = resolve_contracts_dir()
        args = {
            "currency": {"vk": "xian-deployer"},
            "chi_cost": {"initial_rate": 20},
            "rewards": None,
            "dao": None,
            "validator_penalties": None,
        }
        for name, constructor_args in args.items():
            self.app.client.submit(
                (contracts_dir / f"{name}.s.py").read_text(encoding="utf-8"),
                name=name,
                constructor_args=constructor_args,
            )
        self.app.client.submit(
            (contracts_dir / "validators.s.py").read_text(encoding="utf-8"),
            name="validators",
            constructor_args={
                "genesis_nodes": NODES,
                "genesis_registration_fee": 1000,
                "downtime_jail_blocks": 3,
            },
        )

    async def finalize_block(self, height: int, votes):
        timestamp = Timestamp()
        timestamp.seconds = height
        request = Request(
            finalize_block=RequestFinalizeBlock(
                height=height,
                time=timestamp,
                txs=[],
                hash=f"block-{height}".encode("utf-8"),
                decided_last_commit=CommitInfo(round=0, votes=votes),
            )
        )
        response = await deserialize(await self.handler.process("finalize_block", request))
        await self.handler.process("commit", Request(commit=RequestCommit()))
        return response.finalize_block

    def commit(self, absent=(), nil=()):
        flags = {key: BlockIDFlag.BLOCK_ID_FLAG_COMMIT for key in NODES}
        flags.update({key: BlockIDFlag.BLOCK_ID_FLAG_ABSENT for key in absent})
        flags.update({key: BlockIDFlag.BLOCK_ID_FLAG_NIL for key in nil})
        return [vote(key, flag) for key, flag in flags.items()]

    def missed(self, key):
        return self.driver.get(f"validators.missed_blocks:{key}") or 0

    async def test_a_validator_absent_for_the_window_is_jailed_and_loses_its_power(self):
        for height in (1, 2):
            await self.finalize_block(height, self.commit(absent=[NODES[0]]))
        self.assertIn(NODES[0], self.driver.get("validators.active_validators"))

        response = await self.finalize_block(3, self.commit(absent=[NODES[0]]))

        self.assertNotIn(NODES[0], self.driver.get("validators.active_validators"))
        self.assertTrue(self.driver.get(f"validators.jailed:{NODES[0]}"))
        self.assertEqual(self.driver.get(f"validators.jail_reasons:{NODES[0]}"), "downtime")
        updates = {
            bytes(update.pub_key.ed25519).hex(): update.power
            for update in response.validator_updates
        }
        self.assertEqual(updates.get(NODES[0]), 0)

    async def test_a_signature_resets_the_count(self):
        await self.finalize_block(1, self.commit(absent=[NODES[0]]))
        await self.finalize_block(2, self.commit(absent=[NODES[0]]))
        await self.finalize_block(3, self.commit())
        self.assertEqual(self.missed(NODES[0]), 0)
        await self.finalize_block(4, self.commit(absent=[NODES[0]]))
        await self.finalize_block(5, self.commit(absent=[NODES[0]]))

        self.assertIn(NODES[0], self.driver.get("validators.active_validators"))

    async def test_a_nil_vote_counts_as_signed(self):
        for height in (1, 2, 3):
            await self.finalize_block(height, self.commit(nil=[NODES[0]]))

        self.assertEqual(self.missed(NODES[0]), 0)
        self.assertIn(NODES[0], self.driver.get("validators.active_validators"))

    async def test_no_liveness_transaction_runs_while_everyone_signs(self):
        calls = []
        process = self.app.tx_processor.process_system_tx

        def recorded(tx):
            calls.append(tx["payload"]["function"])
            return process(tx)

        with mock.patch.object(self.app.tx_processor, "process_system_tx", recorded):
            await self.finalize_block(1, self.commit())
            await self.finalize_block(2, self.commit(absent=[NODES[1]]))
            await self.finalize_block(3, self.commit())
            await self.finalize_block(4, self.commit())

        # Block 3 closes the open count; blocks 1 and 4 have nothing to record.
        self.assertEqual(calls, ["record_liveness", "record_liveness"])

    async def test_votes_of_unknown_validators_are_ignored(self):
        votes = self.commit() + [vote("55" * 32, BlockIDFlag.BLOCK_ID_FLAG_ABSENT)]

        await self.finalize_block(1, votes)

        self.assertEqual([self.missed(key) for key in NODES], [0, 0, 0, 0])
