from cometbft.abci.v1beta3.types_pb2 import ResponseInitChain
from xian.utils.block import set_latest_block, store_genesis_block


async def init_chain(self, req) -> ResponseInitChain:
    abci_genesis_state = self.genesis["abci_genesis"]
    # ContractingClient may initialize local helper state before genesis. The
    # consensus state root must be derived only from the declared genesis state.
    self.client.raw_driver.flush_full()
    # Await so the genesis write is durable before InitChain returns. The
    # previous fire-and-forget `asyncio.ensure_future(...)` could drop the
    # write if the app crashed or restarted before the coroutine ran.
    await store_genesis_block(
        self.client, self.nonce_storage, abci_genesis_state
    )
    state_root = self.state_root_cache.rebuild(
        self.client.raw_driver.items().items()
    )
    expected_hash = abci_genesis_state.get("hash")
    if expected_hash and bytes.fromhex(expected_hash) != state_root:
        raise ValueError("genesis state root does not match abci_genesis.hash")

    set_latest_block(
        block_hash=state_root,
        height=int(abci_genesis_state.get("number", 0) or 0),
    )

    return ResponseInitChain(app_hash=state_root)
