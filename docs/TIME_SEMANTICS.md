# Time Semantics

Xian contract time is chain time, not wall-clock time.

- `now` inside a contract is derived from the finalized CometBFT block time.
- Every transaction in the same block observes the same `now`.
- `now` is deterministic because it comes from the block header agreed by
  consensus.
- `now` is expressed in UTC and carried into contracts through the `Datetime`
  bridge type.

## Block Policies

Xian supports three deterministic block-production policies:

- `on_demand`: `create_empty_blocks = false`,
  `create_empty_blocks_interval = "0s"`
- `idle_interval`: `create_empty_blocks = false` and a positive
  `create_empty_blocks_interval`
- `periodic`: `create_empty_blocks = true` and a positive
  `create_empty_blocks_interval`

The only semantic difference is when chain time advances during otherwise idle
periods.

The empty-block interval is not an exact finalized block-time target. CometBFT
still applies normal consensus timing, including `timeout_commit`, and Xian must
finish block execution before the next height can finalize. For example, a local
chain with `timeout_commit = "1s"` and
`create_empty_blocks_interval = "1s"` should be expected to finalize idle blocks
at roughly a two-second cadence rather than exactly once per second.

Implications:

- if the chain is idle, contract time does not advance
- time-based state changes do not happen "in the background"
- deadlines and expirations are enforced when a transaction is executed in a
  later block

This is the correct tradeoff for a deterministic chain that does not want
periodic empty blocks unless operators choose that policy explicitly.

## Contract Design Guidance

Contracts should treat time as a condition checked at execution time, not as a
scheduler.

Good patterns:

- `assert now < deadline`
- `assert now >= unlock_time`
- computing accrued value from `(now - start_time)` when a user interacts

Bad assumptions:

- expecting callbacks or autonomous execution at a precise wall-clock moment
- assuming time progresses between blocks

## Precision

Xian preserves CometBFT block timestamp precision up to Python `datetime`
microseconds. Sub-microsecond nanoseconds are truncated because Python's
`datetime` does not support nanosecond storage.

## Simulation

Transaction simulation uses the latest known chain time when block metadata is
available. If no committed block time exists yet, simulation falls back to the
Unix epoch.

This keeps simulation behavior aligned with real execution as closely as
possible while still giving uninitialized local environments a deterministic
starting point.
