## Time Semantics

Xian contract time is chain time, not wall-clock time.

- `now` inside a contract is derived from the finalized CometBFT block time.
- Every transaction in the same block observes the same `now`.
- `now` is deterministic because it comes from the block header agreed by consensus.
- `now` is expressed in UTC and carried into contracts through the `Datetime` bridge type.

## Block Policies

Xian supports three deterministic block-production policies:

- `create_empty_blocks = false`
- `create_empty_blocks_interval = "0s"`

This is `on_demand`. Chain time only advances when CometBFT produces a new
block carrying transactions or a proof block.

Two other valid policies are:

- `idle_interval`: `create_empty_blocks = false` and
  `create_empty_blocks_interval = "10s"` or similar
- `periodic`: `create_empty_blocks = true` and
  `create_empty_blocks_interval = "10s"` or similar

All three are deterministic. The only semantic difference is when chain time
advances during otherwise idle periods.

Implications:

- If the chain is idle, contract time does not advance.
- Time-based state changes do not happen "in the background".
- Deadlines and expirations are enforced when a transaction is executed in a later block.

This is the correct tradeoff for a deterministic chain that does not want
periodic empty blocks.

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
