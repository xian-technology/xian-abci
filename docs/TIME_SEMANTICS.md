## Time Semantics

Xian contract time is chain time, not wall-clock time.

- `now` inside a contract is derived from the finalized CometBFT block time.
- Every transaction in the same block observes the same `now`.
- `now` is deterministic because it comes from the block header agreed by consensus.
- `now` is expressed in UTC and carried into contracts through the `Datetime` bridge type.

## On-Demand Blocks

Xian nodes are configured for on-demand block production:

- `create_empty_blocks = false`
- `create_empty_blocks_interval = "0s"`

In that mode, chain time only advances when CometBFT produces a new block.

Implications:

- If the chain is idle, contract time does not advance.
- Time-based state changes do not happen "in the background".
- Deadlines and expirations are enforced when a transaction is executed in a later block.

This is the correct tradeoff for a deterministic chain that does not want periodic empty blocks.

## Contract Design Guidance

Contracts should treat time as a condition checked at execution time, not as a scheduler.

Good patterns:

- `assert now < deadline`
- `assert now >= unlock_time`
- computing accrued value from `(now - start_time)` when a user interacts

Bad assumptions:

- expecting callbacks or autonomous execution at a precise wall-clock moment
- assuming time progresses between blocks

## Precision

Xian preserves CometBFT block timestamp precision up to Python `datetime` microseconds.
Sub-microsecond nanoseconds are truncated because Python's `datetime` does not support nanosecond storage.

## Simulation

Transaction simulation should use the latest known chain time when block metadata is available.
This keeps simulation behavior aligned with real execution as closely as possible.
