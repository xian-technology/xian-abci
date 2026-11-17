from tendermint.abci.types_pb2 import ResponseInitChain
from xian.evidence_policy import (
    comet_time_nanos,
    evidence_checkpoint_height,
    validate_genesis_evidence_policy,
    validate_init_evidence_request,
)
from xian.execution_policy import validate_genesis_execution_policy
from xian.utils.block import set_latest_block, store_genesis_block


async def init_chain(self, req) -> ResponseInitChain:
    abci_genesis_state = self.genesis["abci_genesis"]
    validate_genesis_execution_policy(abci_genesis_state)
    evidence_policy = validate_genesis_evidence_policy(self.genesis)
    genesis_nanos = comet_time_nanos(self.genesis["genesis_time"])
    genesis_height = evidence_checkpoint_height(abci_genesis_state)
    validate_init_evidence_request(req, evidence_policy, nanos=genesis_nanos, height=genesis_height)
    # ContractingClient may initialize local helper state before genesis. The
    # consensus state root must be derived only from the declared genesis state.
    self.client.raw_driver.flush_full()
    # Await so the genesis write is durable before InitChain returns. The
    # previous fire-and-forget `asyncio.ensure_future(...)` could drop the
    # write if the app crashed or restarted before the coroutine ran.
    await store_genesis_block(self.client, abci_genesis_state)
    self.refresh_tx_fee_policy()
    state_root = self.state_root_cache.rebuild_from_driver(self.client.raw_driver)
    expected_hash = abci_genesis_state.get("hash")
    if expected_hash and bytes.fromhex(expected_hash) != state_root:
        raise ValueError("genesis state root does not match abci_genesis.hash")

    set_latest_block(
        block_hash=state_root,
        height=genesis_height,
        nanos=genesis_nanos,
        storage_home=self.client.raw_driver.storage_home,
    )

    return ResponseInitChain(
        consensus_params=evidence_policy.consensus_params(
            nanos=genesis_nanos, height=genesis_height
        ),
        app_hash=state_root,
        validators=self.validator_handler.genesis_validators(),
    )
